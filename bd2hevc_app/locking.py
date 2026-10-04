"""Kernel-backed cross-process locks, released automatically after a crash."""
from __future__ import annotations
import json
import os
import time
import uuid
from pathlib import Path


class FileLock:
    def __init__(self, path: Path, *, timeout: float = 10):
        self.path, self.timeout, self.handle = path, timeout, None
        self.token = uuid.uuid4().hex

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                handle.seek(1)
                handle.truncate(1)
                handle.write(json.dumps({"pid": os.getpid(), "token": self.token}).encode())
                handle.flush()
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)

    def release(self):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Another process holds the work/state lock: {self.path}")
        return self

    def __exit__(self, *_args):
        self.release()


def inherited_work_slot(path: Path) -> bool:
    token = os.environ.get("BD2HEVC_WORK_TOKEN")
    if not token:
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(1)
            record = json.loads(handle.read())
        return record["token"] == token and int(record["pid"]) == os.getppid()
    except (OSError, ValueError, KeyError):
        return False
