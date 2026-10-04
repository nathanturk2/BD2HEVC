"""Bounded UDF file access and journalled, size-preserving metadata repairs.

Supports the physical partition maps used by BD2HEVC's existing images. Never
guesses how to write an unsupported layout; media extents are left untouched.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import time

from .udf_validation import BLOCK, UdfValidationError, checked_tag, inspect_udf, number


class UdfImage:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.size = self.path.stat().st_size
        self.volume = inspect_udf(self.path)
        descriptors = self.volume["descriptors"]
        partition = next(bytes.fromhex(d["data"]) for d in descriptors if d["tag"] == 5)
        logical = next(bytes.fromhex(d["data"]) for d in descriptors if d["tag"] == 6)
        if number(logical, 264) != 6 or number(logical, 268) != 1 or logical[440:442] != b"\x01\x06":
            raise UdfValidationError("Repair supports a single physical UDF partition only")
        if number(logical, 444, 2) != number(partition, 22, 2):
            raise UdfValidationError("UDF partition map mismatch")
        self.partition = number(partition, 188) * BLOCK
        self.partition_size = number(partition, 192) * BLOCK
        fsd = self.read_at(self.partition + number(logical, 252) * BLOCK, BLOCK)
        if checked_tag(fsd, number(logical, 252)) != 256:
            raise UdfValidationError("Invalid file set descriptor")
        if number(logical, 256, 2) or number(fsd, 408, 2):
            raise UdfValidationError("Unsupported file set partition reference")
        self.files: dict[str, dict] = {}
        self._seen: set[int] = set()
        self._walk(number(fsd, 404), "", 0)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise UdfValidationError("Read outside ISO")
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        if len(data) != length:
            raise UdfValidationError("Short ISO read")
        return data

    def _entry(self, block: int) -> dict:
        offset = self.partition + block * BLOCK
        data = self.read_at(offset, BLOCK)
        tag = checked_tag(data, block)
        if tag not in (261, 266):
            raise UdfValidationError("Not a UDF file entry")
        ext = tag == 266
        size = number(data, 56, 8)
        ea = number(data, 208 if ext else 168)
        alen = number(data, 212 if ext else 172)
        start = (216 if ext else 176) + ea
        mode = number(data, 34, 2) & 7
        if start + alen > BLOCK:
            raise UdfValidationError("Allocation table extends outside file-entry block")
        extents = []
        if mode == 3:
            if size > alen:
                raise UdfValidationError("Truncated embedded file")
            extents = [(offset + start, size)]
        elif mode in (0, 1):
            step = 8 if mode == 0 else 16
            if alen % step:
                raise UdfValidationError("Invalid allocation table length")
            for p in range(start, start + alen, step):
                length = number(data, p)
                if length >> 30:
                    raise UdfValidationError("Sparse/continued UDF allocation is not supported for repair")
                if mode == 1 and number(data, p + 8, 2):
                    raise UdfValidationError("Unsupported allocation partition")
                relative = number(data, p + 4) * BLOCK
                if relative + length > self.partition_size:
                    raise UdfValidationError("Allocation extends outside partition")
                extents.append((self.partition + relative, length))
            if sum(length for _, length in extents) < size:
                raise UdfValidationError("File allocation is shorter than declared content")
        else:
            raise UdfValidationError("Unsupported UDF allocation type")
        return {"size": size, "extents": extents, "directory": data[27] == 4, "embedded": mode == 3}

    def _walk(self, block: int, parent: str, depth: int) -> None:
        if depth > 32 or block in self._seen:
            raise UdfValidationError("Cyclic or excessively nested UDF directory")
        self._seen.add(block)
        entry = self._entry(block)
        if not entry["directory"] or entry["size"] > 16 * 1024 * 1024:
            raise UdfValidationError("Invalid UDF directory")
        data = self.read_entry(entry)
        pos = 0
        while pos < len(data):
            if not any(data[pos:pos + 16]):
                pos = (pos // BLOCK + 1) * BLOCK
                continue
            if checked_tag(data[pos:]) != 257 or pos + 38 > len(data):
                raise UdfValidationError("Invalid UDF directory record")
            flags, nlen = data[pos + 18:pos + 20]
            impl = number(data, pos + 36, 2)
            end = pos + ((38 + impl + nlen + 3) & ~3)
            if end > len(data) or number(data, pos + 28, 2):
                raise UdfValidationError("Invalid directory record bounds/partition")
            if not flags & (8 | 4):
                encoded = data[pos + 38 + impl:pos + 38 + impl + nlen]
                if not encoded or encoded[0] not in (8, 16):
                    raise UdfValidationError("Unsupported UDF filename encoding")
                name = encoded[1:].decode("latin1" if encoded[0] == 8 else "utf-16-be")
                if name in (".", "..") or any(c in name for c in "/\\\0"):
                    raise UdfValidationError("Unsafe UDF filename")
                path = parent + "/" + name if parent else name
                child = number(data, pos + 24)
                if flags & 2:
                    self._walk(child, path, depth + 1)
                else:
                    if path in self.files:
                        raise UdfValidationError("Duplicate UDF file path")
                    self.files[path] = self._entry(child)
            pos = end

    def read_entry(self, entry: dict, limit: int | None = None) -> bytes:
        remaining = entry["size"] if limit is None else min(entry["size"], limit)
        chunks = []
        for offset, length in entry["extents"]:
            take = min(remaining, length)
            if take:
                chunks.append(self.read_at(offset, take))
                remaining -= take
            if not remaining:
                break
        return b"".join(chunks)

    def read(self, path: str, limit: int | None = None) -> bytes:
        entry = self.files[path]
        if limit is None and entry["size"] > 32 * 1024 * 1024:
            raise UdfValidationError("Specify a bounded read for large media files")
        return self.read_entry(entry, limit)

    def replacement(self, path: str, content: bytes) -> list[dict]:
        if not path.startswith(("BDMV/CLIPINF/", "BDMV/PLAYLIST/", "BDMV/BACKUP/CLIPINF/", "BDMV/BACKUP/PLAYLIST/")):
            raise UdfValidationError("Only CLPI/MPLS metadata may be replaced")
        entry = self.files[path]
        if entry["embedded"]:
            raise UdfValidationError("Embedded metadata requires file-entry CRC rewriting; repair refused")
        if len(content) != entry["size"]:
            raise UdfValidationError("In-place repair cannot change file length")
        patches, cursor = [], 0
        for offset, length in entry["extents"]:
            after = content[cursor:cursor + length]
            if not after:
                break
            before = self.read_at(offset, len(after))
            if before != after:
                patches.append({"offset": offset, "before": before.hex(), "after": after.hex(), "reason": path})
            cursor += len(after)
        return patches


def _locked_file(path: Path):
    if os.name == "nt":
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        return os.fdopen(fd, "r+b", buffering=0)
    import fcntl
    handle = path.open("r+b", buffering=0)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def _validate_patches(patches: list[dict], size: int) -> list[dict]:
    patches = sorted(patches, key=lambda p: p["offset"])
    previous_end = 0
    for patch in patches:
        before, after = bytes.fromhex(patch["before"]), bytes.fromhex(patch["after"])
        offset = patch["offset"]
        if not before or len(before) != len(after) or offset < previous_end or offset + len(before) > size:
            raise UdfValidationError("Overlapping, out-of-bounds or resizing repair")
        previous_end = offset + len(before)
    return patches


def apply_patches(path: Path, patches: list[dict], journal_dir: Path, *, expected_stat: tuple[int, int]) -> Path | None:
    """Durably save every original byte before writing. Roll back on failure."""
    if not patches:
        return None
    path = path.resolve()
    size, mtime = expected_stat
    patches = _validate_patches(patches, size)
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = journal_dir / (hashlib.sha256(str(path).encode()).hexdigest()[:20] + f"-{time.time_ns()}.json")
    with _locked_file(path) as handle:
        stat = os.fstat(handle.fileno())
        if (stat.st_size, stat.st_mtime_ns) != (size, mtime):
            raise UdfValidationError("Source changed since inspection; rescan before repair")
        for patch in patches:
            handle.seek(patch["offset"])
            if handle.read(len(bytes.fromhex(patch["before"]))) != bytes.fromhex(patch["before"]):
                raise UdfValidationError("Original bytes changed since inspection")
        value = {"schema": "bd2hevc-byte-repair-v1", "path": str(path), "size": size,
                 "original_mtime_ns": mtime, "state": "prepared", "patches": patches}
        with journal.open("x", encoding="utf-8") as recovery:
            json.dump(value, recovery, indent=2)
            recovery.flush()
            os.fsync(recovery.fileno())
        try:
            for patch in patches:
                handle.seek(patch["offset"])
                handle.write(bytes.fromhex(patch["after"]))
            handle.flush()
            os.fsync(handle.fileno())
            for patch in patches:
                handle.seek(patch["offset"])
                if handle.read(len(bytes.fromhex(patch["after"]))) != bytes.fromhex(patch["after"]):
                    raise UdfValidationError("Repair read-back failed")
        except BaseException:
            for patch in patches:
                handle.seek(patch["offset"])
                handle.write(bytes.fromhex(patch["before"]))
            handle.flush()
            os.fsync(handle.fileno())
            raise
    # The durable prepared journal is sufficient for recovery even if marking
    # completion is interrupted. Never overwrite that journal.
    journal.with_suffix(".applied").write_text(str(time.time_ns()), encoding="ascii")
    return journal


def restore_journal(journal: Path) -> None:
    value = json.loads(journal.read_text(encoding="utf-8"))
    if value.get("schema") != "bd2hevc-byte-repair-v1":
        raise UdfValidationError("Unsupported repair journal")
    path = Path(value["path"])
    value["patches"] = _validate_patches(value["patches"], value["size"])
    with _locked_file(path) as handle:
        if os.fstat(handle.fileno()).st_size != value["size"]:
            raise UdfValidationError("Image size changed since repair")
        for patch in value["patches"]:
            handle.seek(patch["offset"])
            current = handle.read(len(bytes.fromhex(patch["before"])))
            if current not in (bytes.fromhex(patch["before"]), bytes.fromhex(patch["after"])):
                raise UdfValidationError("Refusing to undo subsequent changes")
        for patch in value["patches"]:
            handle.seek(patch["offset"])
            handle.write(bytes.fromhex(patch["before"]))
        handle.flush()
        os.fsync(handle.fileno())
        for patch in value["patches"]:
            handle.seek(patch["offset"])
            if handle.read(len(bytes.fromhex(patch["before"]))) != bytes.fromhex(patch["before"]):
                raise UdfValidationError("Undo read-back failed")
    journal.with_suffix(".restored").write_text(str(time.time_ns()), encoding="ascii")
