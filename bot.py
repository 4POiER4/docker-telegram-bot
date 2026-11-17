import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import RESTART_THRESHOLD, SNAPSHOT, STALE_AFTER, container_names, read_json, write_json


TOKEN_FILE = Path("/run/secrets/telegram_token")
ALERT_STATE = Path("/data/alerts.json")
SOURCE = "@docker"
HELP = "Отправь /status, чтобы посмотреть состояние контейнеров."
STATUS_TEXT = {
    "running": "работает",
    "healthy": "работает, healthcheck в норме",
    "starting": "работает, healthcheck запускается",
    "unhealthy": "не проходит healthcheck",
    "exited": "остановлен",
    "dead": "аварийно завершён",
    "created": "создан, ждёт запуска",
    "paused": "на паузе",
    "restarting": "перезапускается",
    "removing": "удаляется",
    "missing": "не найден",
}


class TelegramError(Exception):
    def __init__(self, code, retry_after=5):
        super().__init__(f"Telegram API ({code})")
        self.code = code
        self.retry_after = max(5, retry_after)


class Telegram:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **payload):
        request = Request(
            self.url + method,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=payload.get("timeout", 0) + 10) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                try:
                    body = json.load(error)
                except (OSError, ValueError):
                    body = {}
            raise TelegramError(error.code, body.get("parameters", {}).get("retry_after", 5)) from None
        except (URLError, OSError, ValueError) as error:
            raise TelegramError(type(error).__name__) from None
        if not result["ok"]:
            raise TelegramError(result.get("error_code", "ошибка"), result.get("parameters", {}).get("retry_after", 5))
        return result["result"]

    def updates(self, offset):
        cursor = {} if offset is None else {"offset": offset}
        return self.call("getUpdates", timeout=5, allowed_updates=["message"], **cursor)

    def send(self, chat_id, header, lines=()):
        # Leave room under Telegram's 4096-character limit, including emoji.
        text = header
        for line in lines:
            if len(text) + len(line) + 1 > 1900:
                self.call("sendMessage", chat_id=chat_id, text=text)
                text = header
            text += "\n" + line
        self.call("sendMessage", chat_id=chat_id, text=text)


def connect_chat(telegram):
    account = telegram.call("getMe")
    code = secrets.token_hex(6)
    command = f"/start {code}"
    print(f"Открой https://t.me/{account['username']}?start={code} и нажми «Запустить».")
    print(f"Можно отправить боту вручную: {command}")
    offset = None
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        for update in telegram.updates(offset):
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") == "private" and message.get("text", "").strip() == command:
                print(f"Добавь в .env строку TELEGRAM_CHAT_ID={chat['id']}")
                return
    raise SystemExit("Время вышло. Запусти команду chat-id снова.")


def current_snapshot(names):
    try:
        snapshot = read_json(SNAPSHOT)
    except (OSError, ValueError):
        return None, "не удалось прочитать данные сборщика"
    if snapshot is None:
        return None, "сборщик ещё не передал данные"
    if time.time() - snapshot["checked_at"] > STALE_AFTER:
        return None, "данные сборщика устарели"
    containers = snapshot["containers"]
    if containers is None:
        return None, "сборщик не может прочитать Docker API"
    if containers.keys() != set(names):
        return None, "сборщик ещё не обновил список контейнеров"
    return containers, None


def describe(container):
    status = container["health"] or container["status"]
    count = sum(delta for _, delta in container["restarts"])
    label = STATUS_TEXT.get(status, status)
    if count:
        label += f" · авторестартов за 5 мин: {count}"
    if count >= RESTART_THRESHOLD:
        issue = "frequent_restarts"
        if status not in {"running", "healthy", "starting"}:
            issue += f":{status}"
        return issue, f"частые перезапуски · {label}"
    return status, label


def status_lines(containers):
    for name, container in containers.items():
        status, label = describe(container)
        icon = "🟢" if status in {"running", "healthy"} else "🟡" if status == "starting" else "🔴"
        yield f"{icon} {name} — {label}"


def alert_changes(containers, error, reported):
    active = dict(reported)
    if error:
        active[SOURCE] = "unavailable"
        lines = [f"⚠️ Мониторинг Docker: {error}."] if SOURCE not in reported else []
        return active, lines

    lines = []
    if active.pop(SOURCE, None):
        lines.append("✅ Мониторинг Docker снова получает данные.")
    for name, container in containers.items():
        status, label = describe(container)
        if status == "starting":
            continue
        if status in {"healthy", "running"}:
            if active.pop(name, None):
                lines.append(f"✅ {name} восстановился — {label}.")
        elif active.get(name) != status:
            active[name] = status
            lines.append(f"🔴 {name} — {label}.")
    return active, lines


def handle_update(telegram, chat_id, title, names, update):
    message = update.get("message", {})
    chat = message.get("chat", {})
    if chat.get("id") != chat_id or chat.get("type") != "private":
        return
    text = message.get("text", "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    if command in {"/start", "/help"}:
        telegram.send(chat_id, HELP)
    elif command == "/status":
        containers, error = current_snapshot(names)
        lines = [f"⚠️ {error}."] if error else status_lines(containers)
        telegram.send(chat_id, f"Контейнеры · {title}", lines)


def monitor(telegram, chat_id, names, title):
    saved = read_json(ALERT_STATE) or {}
    reported = {}
    if saved.get("chat_id") == chat_id:
        reported = {
            name: state for name, state in saved["reported"].items()
            if name in names or name == SOURCE
        }
    started = time.monotonic()
    offset = None
    logging.info("Бот запущен. Отправь /status в личном чате.")
    while True:
        try:
            containers, error = current_snapshot(names)
            if error is None or time.monotonic() - started >= STALE_AFTER:
                active, lines = alert_changes(containers, error, reported)
                if lines:
                    telegram.send(chat_id, f"Docker · {title}", lines)
                    write_json(ALERT_STATE, {"chat_id": chat_id, "reported": active})
                    reported = active
            for update in telegram.updates(offset):
                handle_update(telegram, chat_id, title, names, update)
                offset = update["update_id"] + 1
        except TelegramError as error:
            if error.code in {401, 404, 409}:
                raise
            logging.warning("%s. Повтор через %s с.", error, error.retry_after)
            time.sleep(error.retry_after)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Запиши токен от @BotFather в private/telegram_token.")
        telegram = Telegram(token)
        if sys.argv[1:] == ["chat-id"]:
            connect_chat(telegram)
            return
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not re.fullmatch(r"[1-9]\d*", chat_id):
            raise ValueError("Укажи TELEGRAM_CHAT_ID в .env. См. команду chat-id в README.")
        title = os.environ.get("SERVER_NAME", "").strip()[:60] or "Сервер"
        monitor(telegram, int(chat_id), container_names(), title)
    except OSError as error:
        raise SystemExit(f"Ошибка доступа к файлу: {error.filename or type(error).__name__}") from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except TelegramError as error:
        hint = {
            401: " Проверь токен от @BotFather.",
            404: " Проверь токен от @BotFather.",
            409: " Используй отдельного бота без webhook и других запущенных копий.",
        }.get(error.code, " Проверь доступ сервера к api.telegram.org.")
        raise SystemExit(f"{error}.{hint}") from None


if __name__ == "__main__":
    main()
