from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
import uuid


class LockHeld(RuntimeError):
    pass


@dataclass(frozen=True)
class ActionRecord:
    action: str
    result: str
    detail: dict


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_ledger(path: Path, record: ActionRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": str(uuid.uuid4()),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": record.action,
        "result": record.result,
        "detail": record.detail,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def single_flight(lock_dir: Path, name: str = "commander.lock"):
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / name
    payload = f"pid={os.getpid()} time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockHeld(f"Commander lock already exists: {lock_path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
