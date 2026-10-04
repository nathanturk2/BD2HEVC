"""UDF 2.50 Blu-ray ISO authoring and verification.

Blu-ray file-system images require UDF 2.50.  The bundled Hadris command-line
writer is used instead of a generic ISO-9660 author so large M2TS files and
BDMV navigation data remain representable.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .iso_integrity import verify_payload
from .runtime_support import publish_file
from .config import ROOT
from .output import validate_output_available
from .tools import ToolError
from .udf_validation import UdfValidationError, validate_udf


def bundled_udf_tool() -> Path:
    return ROOT / "tools" / "hadris-udf" / "bin" / ("hadris-udf.exe" if os.name == "nt" else "hadris-udf")


def find_udf_tool(explicit: str | Path | None = None) -> Path:
    candidates: list[str | Path] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("BD2HEVC_UDF_TOOL"):
        candidates.append(os.environ["BD2HEVC_UDF_TOOL"])
    candidates.append(bundled_udf_tool())
    for name in ("hadris-udf", "hadris-udf.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    raise ToolError(
        "A UDF 2.50 authoring tool was not found. Reinstall BD2HEVC with its "
        "bundled tools, set BD2HEVC_UDF_TOOL, or pass --iso-author-tool."
    )


def validate_bluray_folder(source: Path) -> None:
    required = (
        source / "BDMV" / "index.bdmv",
        source / "BDMV" / "MovieObject.bdmv",
        source / "BDMV" / "PLAYLIST",
        source / "BDMV" / "STREAM",
    )
    missing = [str(path.relative_to(source)) for path in required if not path.exists()]
    if missing:
        raise ToolError(f"Not a complete Blu-ray folder backup ({', '.join(missing)} missing): {source}")
    if not any((source / "BDMV" / "STREAM").glob("*.m2ts")):
        raise ToolError(f"Blu-ray STREAM folder contains no M2TS files: {source}")


def folder_stats(source: Path) -> tuple[int, int, int]:
    files = 0
    directories = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(source):
        directories += len(dirnames)
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            files += 1
            total_bytes += path.stat().st_size
    return files, directories, total_bytes


def iso_output_path(path: str | Path) -> Path:
    output = Path(path).expanduser()
    if output.suffix.casefold() != ".iso":
        output = output.with_name(output.name + ".iso")
    return output.resolve()


def iso_staging_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.bd2hevc-folder.part")


def iso_partial_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.bd2hevc-iso.part")


def iso_progress_path(partial: Path) -> Path:
    return partial.with_name(partial.name + ".progress")


def volume_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_-").upper()
    return (label or "BD2HEVC_DISC")[:32]


def _run(tool: Path, arguments: list[str], *, verbose: bool = False) -> subprocess.CompletedProcess[str]:
    command = [str(tool), *arguments]
    if verbose:
        print("ISO: " + subprocess.list2cmdline(command), flush=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        check=False,
    )
    if verbose and completed.stdout:
        print(completed.stdout.rstrip(), flush=True)
    if completed.returncode:
        tail = "\n".join((completed.stdout or "").splitlines()[-30:])
        raise ToolError(f"UDF authoring tool failed ({completed.returncode}).\n{tail}")
    return completed


def verify_bluray_iso(
    image: str | Path,
    *,
    tool: str | Path | None = None,
    expected_files: int | None = None,
    expected_directories: int | None = None,
    reference: str | Path | None = None,
    manifest: str | Path | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    image_path = Path(image).expanduser().resolve()
    if not image_path.is_file() or image_path.stat().st_size <= 0:
        raise ToolError(f"ISO image is missing or empty: {image_path}")
    try:
        independent = validate_udf(image_path)
    except UdfValidationError as exc:
        raise ToolError(f"ISO filesystem compatibility check failed: {exc}") from exc
    udf_tool = find_udf_tool(tool)
    verification = _run(udf_tool, ["verify", str(image_path), "--verbose"], verbose=verbose)
    info = _run(udf_tool, ["info", str(image_path)], verbose=verbose)
    combined = (verification.stdout or "") + "\n" + (info.stdout or "")
    if not re.search(r"UDF\s+revision:\s*2\.50", combined, re.IGNORECASE):
        raise ToolError(f"Authored image is not reported as UDF 2.50: {image_path}")
    errors = re.search(r"(\d+)\s+errors?", verification.stdout or "", re.IGNORECASE)
    if errors and int(errors.group(1)) != 0:
        raise ToolError(f"UDF verification reported errors for {image_path}")
    tree = re.search(r"Directory tree:\s*(\d+)\s+files?,\s*(\d+)\s+directories", verification.stdout or "", re.IGNORECASE)
    actual_files = int(tree.group(1)) if tree else None
    actual_directories = int(tree.group(2)) if tree else None
    if expected_files is not None and actual_files != expected_files:
        raise ToolError(f"ISO file-count mismatch: expected {expected_files}, found {actual_files}")
    if expected_directories is not None and actual_directories != expected_directories:
        raise ToolError(f"ISO directory-count mismatch: expected {expected_directories}, found {actual_directories}")
    payload = None
    if reference is not None or manifest is not None:
        try:
            payload = verify_payload(image_path, source=Path(reference) if reference else None, manifest=Path(manifest) if manifest else None)
        except (ValueError, KeyError, TypeError, OSError, UdfValidationError) as exc:
            raise ToolError(f"ISO payload verification failed: {exc}") from exc
    return {
        "ok": True,
        "payload_manifest": payload,
        "verification_scope": "filesystem-and-payload" if payload else "filesystem",
        "image": str(image_path),
        "bytes": image_path.stat().st_size,
        "udf_revision": "2.50",
        "files": actual_files,
        "directories": actual_directories,
        "tool": str(udf_tool),
        "independent_descriptors": independent,
    }


def author_bluray_iso(
    source: str | Path,
    output: str | Path,
    *,
    tool: str | Path | None = None,
    label: str | None = None,
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    validate_bluray_folder(source_path)
    output_path = iso_output_path(output)
    validate_output_available(output_path, source_path, force=force)
    if output_path.is_dir():
        raise ToolError(f"Output ISO path is a directory: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    files, directories, source_bytes = folder_stats(source_path)
    required_free = source_bytes + max(32 * 1024 * 1024, source_bytes // 100)
    free = shutil.disk_usage(output_path.parent).free
    if free < required_free:
        raise ToolError(
            f"Not enough free space to author {output_path.name}: need about "
            f"{required_free / 1_000_000_000:.2f} GB, have {free / 1_000_000_000:.2f} GB"
        )
    udf_tool = find_udf_tool(tool)
    partial = iso_partial_path(output_path)
    progress = iso_progress_path(partial)
    partial.unlink(missing_ok=True)
    progress.unlink(missing_ok=True)
    try:
        creation = _run(
            udf_tool,
            [
                "create",
                str(source_path),
                "--output",
                str(partial),
                "--volume-name",
                volume_label(label or source_path.name),
                "--revision",
                "2.50",
                "--progress-file",
                str(progress),
                "--verbose",
            ],
            verbose=verbose,
        )
        created_files = re.search(r"(?:Files:\s*|Found\s+)(\d+)\s+files?", creation.stdout or "", re.IGNORECASE)
        if created_files is None or int(created_files.group(1)) != files:
            found = created_files.group(1) if created_files else "unknown"
            raise ToolError(f"UDF author reported {found} files; expected {files}")
        verification = verify_bluray_iso(
            partial,
            tool=udf_tool,
            expected_files=files,
            expected_directories=directories,
            verbose=verbose,
        )
        verification["payload_manifest"] = verify_payload(partial, source=source_path)
        verification["verification_scope"] = "filesystem-and-payload"
        if partial.stat().st_size < source_bytes:
            raise ToolError("Authored ISO is smaller than its contained file payload")
        if output_path.exists() and not force:
            raise ToolError(f"Output ISO already exists: {output_path}. Use --force to replace it.")
        publish_file(partial, output_path, force=force)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        progress.unlink(missing_ok=True)
    manifest_path = output_path.with_name(output_path.name + ".sha256.json")
    temporary_manifest = manifest_path.with_name("." + manifest_path.name + ".tmp")
    try:
        temporary_manifest.write_text(json.dumps(verification["payload_manifest"], indent=2), encoding="utf-8")
        publish_file(temporary_manifest, manifest_path, force=True)
        verification["manifest_path"] = str(manifest_path)
    except OSError as exc:
        verification["manifest_warning"] = f"Payload was verified; checksum sidecar could not be saved: {exc}"
    finally:
        temporary_manifest.unlink(missing_ok=True)
    verification["image"] = str(output_path)
    verification["bytes"] = output_path.stat().st_size
    verification["source"] = str(source_path)
    verification["source_bytes"] = source_bytes
    verification["files"] = files
    verification["directories"] = directories
    verification["volume_label"] = volume_label(label or source_path.name)
    return verification
