"""Interactive VLC/libbluray debug recording helpers."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import DEFAULT_REPORT_DIR, VERSION
from .diagnostics import (
    collect_tool_summary,
    copy_redacted_report,
    redact_text,
    redact_value,
    redaction_map,
    safe_report_name,
    summarize_disc_tree,
    write_log_highlights,
)
from .scan import find_disc_roots
from .tools import ToolError, format_cmd, refreshed_env


def bluray_uri(root: Path) -> str:
    return "bluray:///" + root.resolve().as_posix()


def isolated_bdj_storage_env(base_dir: Path, label: str, *, create: bool = True) -> dict[str, str]:
    storage_root = base_dir.resolve() / safe_report_name(label)
    cache_root = storage_root / "cache"
    persistent_root = storage_root / "persistent"
    if create:
        cache_root.mkdir(parents=True, exist_ok=True)
        persistent_root.mkdir(parents=True, exist_ok=True)
    return {
        "LIBBLURAY_CACHE_ROOT": str(cache_root),
        "LIBBLURAY_PERSISTENT_ROOT": str(persistent_root),
    }


def build_vlc_libbluray_record_command(
    *,
    vlc: str,
    root: Path,
    log_path: Path,
    region: str | None = None,
    verbose_level: int = 3,
) -> list[str]:
    cmd = [
        vlc,
        "--no-one-instance",
        "--no-playlist-enqueue",
        "--no-qt-privacy-ask",
        "--qt-continue=0",
        "--no-video-title-show",
        "--file-logging",
        f"--logfile={log_path}",
        f"--verbose={verbose_level}",
        "--bluray-menu",
    ]
    if region:
        cmd.append(f"--bluray-region={region.upper()}")
    cmd.append(bluray_uri(root))
    return cmd


def _resolve_bdmv_root(path: Path) -> Path:
    roots = find_disc_roots([path.resolve()])
    if not roots:
        raise ToolError(f"No BDMV folder found at {path}")
    return roots[0].resolve()


def _copy_log_text(source: Path, destination: Path, mapping: dict[str, str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        destination.write_text("VLC did not write a log file.\n", encoding="utf-8")
        return
    copy_redacted_report(source, destination, mapping)


def _zip_folder(folder: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder).as_posix())


def write_libbluray_record_readme(destination: Path) -> None:
    destination.write_text(
        "\n".join(
            [
                "BD2HEVC VLC/libbluray debug recording",
                "",
                "This bundle captures an interactive VLC reproduction session plus safe disc metadata.",
                "It is intended for debugging BD-J/menu/gallery issues that only appear during real navigation.",
                "",
                "Included:",
                "- Redacted VLC/libbluray log from the reproduction session.",
                "- Tool versions and platform details.",
                "- File manifests with names, sizes, and timestamps only.",
                "- Optional reference/source manifest if --source was supplied.",
                "- Error-highlight extracts from the VLC log.",
                "",
                "Not included:",
                "- .m2ts media, BD-J JAR contents, keys, decryption logs, or raw disc assets.",
                "",
                "When opening a GitHub issue, also describe the exact button sequence that caused the failure.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def create_libbluray_recording(
    *,
    target: Path,
    vlc: str,
    source: Path | None = None,
    label: str | None = None,
    output_dir: Path | None = None,
    region: str | None = None,
    duration: float | None = None,
    verbose_level: int = 3,
    isolated_bdj_storage: bool = False,
    dry_run: bool = False,
    keep_folder: bool = False,
) -> dict[str, Any]:
    root = _resolve_bdmv_root(target)
    source_root = _resolve_bdmv_root(source) if source else None
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    base_name = safe_report_name(label or root.parent.name or root.name)
    recordings_dir = (output_dir or DEFAULT_REPORT_DIR / "libbluray-recordings").resolve()
    session_dir = recordings_dir / f"{base_name}-{timestamp}"
    zip_path = recordings_dir / f"{session_dir.name}.zip"
    vlc_log = session_dir / "vlc-libbluray.log"
    mapping = redaction_map([path for path in [root, root.parent, source_root, source_root.parent if source_root else None, session_dir] if path])
    cmd = build_vlc_libbluray_record_command(vlc=vlc, root=root, log_path=vlc_log, region=region, verbose_level=verbose_level)
    bdj_env = isolated_bdj_storage_env(session_dir / "bdj-storage", root.parent.name, create=False) if isolated_bdj_storage else {}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "target": str(root),
            "source": str(source_root) if source_root else None,
            "command": format_cmd(cmd),
            "isolated_bdj_storage": isolated_bdj_storage,
            "bdj_storage_env": bdj_env,
            "session_folder": str(session_dir),
            "bundle": str(zip_path),
        }

    with tempfile.TemporaryDirectory(prefix="bd2hevc-libbluray-record-") as temp:
        workdir = Path(temp) / session_dir.name
        workdir.mkdir(parents=True)
        live_log = workdir / "vlc-libbluray.log"
        cmd = build_vlc_libbluray_record_command(vlc=vlc, root=root, log_path=live_log, region=region, verbose_level=verbose_level)
        bdj_env = isolated_bdj_storage_env(workdir / "bdj-storage", root.parent.name) if isolated_bdj_storage else {}
        started = time.time()
        write_libbluray_record_readme(workdir / "README.txt")

        print("Opening VLC with verbose libbluray logging.")
        print("Reproduce the failing menu/gallery path in VLC.")
        if isolated_bdj_storage:
            print("Using isolated libbluray BD-J cache/persistent storage for this recording.")
        if duration:
            print(f"Recording will stop automatically after {duration:g} seconds.")
        else:
            print("When the failure has happened, return here and press Enter to finish the recording.")
        env = refreshed_env()
        env.update(bdj_env)
        proc = subprocess.Popen(cmd, env=env)
        if duration:
            try:
                proc.wait(timeout=duration)
            except subprocess.TimeoutExpired:
                pass
        else:
            try:
                input()
            except EOFError:
                pass

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        ended = time.time()

        mapping = redaction_map([path for path in [root, root.parent, source_root, source_root.parent if source_root else None, workdir, live_log] if path])
        session: dict[str, Any] = {
            "bd2hevc_version": VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "duration_seconds": round(ended - started, 3),
            "platform": platform.platform(),
            "system": platform.system(),
            "python": sys.version,
            "target": redact_value(summarize_disc_tree(root), mapping),
            "source": redact_value(summarize_disc_tree(source_root), mapping) if source_root else None,
            "vlc": {
                "returncode": proc.returncode,
                "region": region,
                "verbose_level": verbose_level,
                "isolated_bdj_storage": isolated_bdj_storage,
                "bdj_storage_env": redact_value(bdj_env, mapping),
                "command": redact_text(format_cmd(cmd), mapping),
                "log": "logs/vlc-libbluray.log",
            },
            "tools": redact_value(collect_tool_summary(), mapping),
        }
        (workdir / "recording.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
        _copy_log_text(live_log, workdir / "logs" / "vlc-libbluray.log", mapping)
        write_log_highlights(live_log, workdir / "logs" / "vlc-libbluray.error-highlights.txt", mapping)

        recordings_dir.mkdir(parents=True, exist_ok=True)
        if keep_folder:
            if session_dir.exists():
                shutil.rmtree(session_dir)
            shutil.copytree(workdir, session_dir)
            bundle_path = session_dir
            zipped = False
        else:
            _zip_folder(workdir, zip_path)
            bundle_path = zip_path
            zipped = True

    return {
        "ok": True,
        "dry_run": False,
        "bundle": str(bundle_path),
        "zipped": zipped,
        "target": str(root),
        "source": str(source_root) if source_root else None,
        "vlc_returncode": proc.returncode,
    }
