"""Streaming file-level integrity for authored Blu-ray UDF images."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .tools import ToolError
from .udf_repair import UdfImage


def verify_payload(image: Path, *, source: Path | None = None, manifest: Path | dict | None = None) -> dict:
    udf = UdfImage(image)
    expected = None
    if source is not None:
        source = source.resolve()
        expected = {}
        for path in source.rglob("*"):
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(8 * 1024 * 1024): digest.update(chunk)
                expected[path.relative_to(source).as_posix()] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    elif manifest is not None:
        value = json.loads(manifest.read_text(encoding="utf-8-sig")) if isinstance(manifest, Path) else manifest
        if value.get("schema") != "bd2hevc-payload-sha256-v1":
            raise ToolError("Unsupported Blu-ray payload manifest")
        expected = {row["path"]: row for row in value["files"]}
    if expected is not None and set(expected) != set(udf.files):
        raise ToolError("Authored ISO payload inventory differs from its reference")
    rows = []
    with image.open("rb") as handle:
        for name, entry in sorted(udf.files.items()):
            digest = hashlib.sha256()
            remaining = entry["size"]
            for offset, length in entry["extents"]:
                handle.seek(offset)
                count = min(remaining, length)
                while count:
                    chunk = handle.read(min(count, 8 * 1024 * 1024))
                    if not chunk: raise ToolError(f"Truncated ISO payload: {name}")
                    digest.update(chunk)
                    count -= len(chunk)
                    remaining -= len(chunk)
            if remaining: raise ToolError(f"Incomplete ISO allocation: {name}")
            row = {"path": name, "size": entry["size"], "sha256": digest.hexdigest()}
            if expected is not None and any(row[key] != expected[name][key] for key in ("size", "sha256")):
                raise ToolError(f"ISO payload hash mismatch: {name}")
            rows.append(row)
    return {"schema": "bd2hevc-payload-sha256-v1", "files": rows, "verified_against_reference": expected is not None}
