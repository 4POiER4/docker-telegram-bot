import http.client
import json
import logging
import socket
import time

from common import CHECK_INTERVAL, RESTART_WINDOW, SNAPSHOT, container_names, read_json, write_json


class DockerConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost", timeout=3)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def inspect(container, socket_path="/docker.sock"):
    connection = DockerConnection(socket_path)
    try:
        connection.request("GET", f"/containers/{container}/json")
        response = connection.getresponse()
        if response.status == 404:
            return None
        if response.status != 200:
            raise OSError(f"Docker API HTTP {response.status}")
        return json.load(response)
    finally:
        connection.close()


def summarize(details, previous, now):
    if details is None:
        return {"status": "missing", "health": "", "restarts": []}

    state = details["State"]
    status = state["Status"]
    health = (state.get("Health") or {}).get("Status", "") if status == "running" else ""
    count = details["RestartCount"]
    restarts = []
    if previous.get("id") == details["Id"]:
        restarts = [
            (seen, delta) for seen, delta in previous["restarts"] if seen > now - RESTART_WINDOW
        ]
        if count > previous["count"] and previous["checked_at"] > now - RESTART_WINDOW:
            restarts.append((now, count - previous["count"]))
    return {
        "id": details["Id"],
        "checked_at": now,
        "status": status,
        "health": health,
        "count": count,
        "restarts": restarts,
    }


def collect(names, previous, get_details=inspect):
    try:
        containers = {
            name: summarize(get_details(name), previous.get(name, {}), time.time())
            for name in names
        }
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        logging.warning("Не удалось прочитать Docker API (%s).", type(error).__name__)
        return {"checked_at": time.time(), "containers": None}
    return {"checked_at": time.time(), "containers": containers}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        names = container_names()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    previous = (read_json(SNAPSHOT) or {}).get("containers") or {}
    logging.info("Сбор состояния запущен. Контейнеров в списке: %s.", len(names))
    while True:
        started = time.monotonic()
        snapshot = collect(names, previous)
        write_json(SNAPSHOT, snapshot)
        if snapshot["containers"] is not None:
            previous = snapshot["containers"]
        time.sleep(max(0, CHECK_INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
