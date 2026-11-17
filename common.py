import json
import os
import re
from pathlib import Path


SNAPSHOT = Path("/snapshot/status.json")
CHECK_INTERVAL = 10
STALE_AFTER = 30
RESTART_WINDOW = 300
RESTART_THRESHOLD = 3


def container_names():
    names = tuple(dict.fromkeys(re.split(r"[,\s]+", os.environ.get("WATCH_CONTAINERS", "").strip())))
    if not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) for name in names):
        raise ValueError("Укажи имена контейнеров в WATCH_CONTAINERS в файле .env.")
    return names


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
