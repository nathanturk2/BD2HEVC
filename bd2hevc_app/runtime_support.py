"""Shared filesystem safety and bounded log reading. Keep both projects in sync."""

from __future__ import annotations

import os
import shutil
import uuid
import json
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

_PROGRESS_CACHE: OrderedDict = OrderedDict()


def read_progress_log(path: Path, prefix: str, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    """Keep progress markers while scanning only newly appended log bytes.

    Raw tool output stays bounded. Terminal and latest per-task events survive
    long logs; truncation/rotation resets the cache. UTF-16 logs are supported.
    """
    try:
        stat = path.stat()
        key = (str(path.resolve()), prefix)
        cached = _PROGRESS_CACHE.get(key)
        identity = (stat.st_dev, stat.st_ino)
        if cached is None or cached["identity"] != identity or stat.st_size < cached["offset"] or (
            stat.st_size == cached["offset"] and stat.st_mtime_ns != cached["mtime"]):
            with path.open("rb") as stream: head = stream.read(4)
            encoding = "utf-16-be" if head.startswith(b"\xfe\xff") else (
                "utf-16-le" if head.startswith(b"\xff\xfe") or b"\x00" in head else "utf-8")
            cached = {"identity":identity,"offset":0,"mtime":0,"pending":b"","encoding":encoding,"markers":OrderedDict()}
            _PROGRESS_CACHE[key] = cached
        encoding = cached["encoding"]
        delimiter = "\n".encode(encoding)
        with path.open("rb") as stream:
            stream.seek(cached["offset"])
            while chunk := stream.read(1024 * 1024):
                start = cached["offset"] - len(cached["pending"])
                cached["offset"] += len(chunk)
                pieces = (cached["pending"] + chunk).split(delimiter)
                cached["pending"] = pieces.pop()
                for raw in pieces:
                    line = raw.decode(encoding, errors="replace").strip("\ufeff\r")
                    marker = line.find(prefix)
                    if marker >= 0:
                        event = line[marker:]
                        if prefix.startswith("DVD"):
                            try:
                                value = json.loads(event[len(prefix):])
                                marker_key = (value.get("lane"),value.get("task"),value.get("event"),value.get("scope"))
                            except (ValueError, AttributeError): marker_key = event
                        else: marker_key = tuple(event.split()[:3])
                        cached["markers"].pop(marker_key, None)
                        cached["markers"][marker_key] = (start, event)
                        if len(cached["markers"]) > 16384: cached["markers"].popitem(last=False)
                    start += len(raw) + len(delimiter)
                if len(cached["pending"]) > max_bytes: cached["pending"] = cached["pending"][-max_bytes:]
        cached["mtime"] = stat.st_mtime_ns
        _PROGRESS_CACHE.move_to_end(key)
        if len(_PROGRESS_CACHE) > 16: _PROGRESS_CACHE.popitem(last=False)
        tail = read_log_tail(path, 20000, max_bytes=max_bytes)
        boundary = max(0, stat.st_size - max_bytes)
        present = {line[line.find(prefix):] for line in tail.splitlines() if prefix in line}
        earlier = [line for position,line in cached["markers"].values() if position < boundary and line not in present]
        return "\n".join(earlier) + "\n" + tail
    except OSError:
        return ""


def reject_overlap(output: Path, protected: Iterable[Path]) -> None:
    destination = output.expanduser().resolve()
    for item in protected:
        source = item.expanduser().resolve()
        if destination == source or source in destination.parents or destination in source.parents:
            raise ValueError(f"Output overlaps a protected input or workspace: {destination}")


def publish_file(temporary: Path, destination: Path, *, force: bool = False) -> None:
    """Publish a completed sibling file; no-overwrite is atomic across producers."""
    if temporary.resolve().parent != destination.resolve().parent:
        raise ValueError("Publication requires a temporary file beside the destination")
    if force:
        if destination.is_dir():
            raise ValueError(f"File output is a directory: {destination}")
        os.replace(temporary, destination)
    else:
        if os.name == "nt":
            # Windows rename refuses an existing destination, including on FAT.
            temporary.rename(destination)
        else:
            os.link(temporary, destination)  # EEXIST leaves old output and temp intact.
            temporary.unlink()


def swap_directory(staging: Path, destination: Path, *, force: bool = False) -> None:
    """Recoverable same-volume swap; preserve the old output until publication."""
    staging, destination = staging.resolve(), destination.resolve()
    if staging.parent != destination.parent or staging == destination:
        raise ValueError("Directory publication requires distinct sibling paths")
    previous = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
    replaced = False
    if destination.exists():
        if not force:
            raise FileExistsError(f"Output already exists: {destination}")
        if not destination.is_dir():
            raise ValueError(f"Directory output is a file: {destination}")
        destination.rename(previous)
        replaced = True
    try:
        staging.rename(destination)
    except BaseException:
        if replaced:
            previous.rename(destination)
        raise
    if replaced:
        # UUID sibling was created by this operation, never a computed collection root.
        if previous.parent != destination.parent or previous == destination:
            raise ValueError("Unsafe previous-output cleanup")
        shutil.rmtree(previous)


def read_log_tail(path: Path, lines: int = 5000, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    """Read a bounded suffix, including UTF-16 PowerShell logs."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            start = max(0, length - max_bytes)
            utf16 = head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head
            if utf16:
                start -= start % 2
            handle.seek(start)
            raw = handle.read(max_bytes + 2)
        encoding = "utf-16-be" if head.startswith(b"\xfe\xff") else ("utf-16-le" if utf16 else "utf-8-sig")
        text = raw.decode(encoding, errors="replace").lstrip("\ufeff")
        rows = text.splitlines()
        if start and rows:
            rows = rows[1:]
        return "\n".join(rows[-max(1, lines):]) + ("\n" if rows else "")
    except OSError:
        return ""
