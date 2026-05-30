"""External tool discovery and process helpers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import HARDWARE_HEVC_ENCODERS, HEVC_ENCODERS, LOCAL_FFMPEG_DIRS, LOCAL_TSMUXERS, MAKEMKV_DIRS, VLC_DIRS


class ToolError(RuntimeError):
    pass


def refreshed_env() -> dict[str, str]:
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    extra_dirs = [p.parent for p in LOCAL_TSMUXERS if usable_local_tool(p)]
    extra_dirs += [p for p in LOCAL_FFMPEG_DIRS if p.exists()]
    if os.name == "nt":
        extra_dirs += MAKEMKV_DIRS + VLC_DIRS
    for folder in extra_dirs:
        try:
            folder_exists = folder.exists()
            folder_resolved = folder.resolve() if folder_exists else folder
        except OSError:
            continue
        if folder_exists and not any(same_existing_path(p, folder_resolved) for p in path_parts):
            path_parts.append(str(folder))
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def same_existing_path(path_value: str, expected: Path) -> bool:
    try:
        path = Path(path_value)
        return path.exists() and path.resolve() == expected
    except (NotImplementedError, OSError):
        return False


def usable_local_tool(path: Path) -> bool:
    if not path.exists():
        return False
    if os.name == "nt":
        return True
    if path.suffix.lower() == ".exe":
        return False
    return os.access(path, os.X_OK)


def which(name: str, extra: list[Path] | None = None) -> str | None:
    if os.name != "nt" and name.lower().endswith(".exe"):
        return None
    env = refreshed_env()
    found = shutil_which(name, path=env.get("PATH"))
    if found:
        return found
    if extra:
        for folder in extra:
            candidate = folder / name
            if candidate.exists():
                return str(candidate)
    return None


def shutil_which(name: str, *, path: str | None = None) -> str | None:
    from shutil import which as _which

    return _which(name, path=path)


def discover_tools() -> dict[str, Any]:
    local_tsmuxer = next((p for p in LOCAL_TSMUXERS if usable_local_tool(p)), None)
    if os.name == "nt":
        ffmpeg = which("ffmpeg.exe") or which("ffmpeg")
        ffprobe = which("ffprobe.exe") or which("ffprobe")
        makemkvcon64 = which("makemkvcon64.exe", MAKEMKV_DIRS) or which("makemkvcon64")
        makemkvcon = which("makemkvcon.exe", MAKEMKV_DIRS) or which("makemkvcon")
        tsmuxer = str(local_tsmuxer) if local_tsmuxer else (which("tsmuxer.exe") or which("tsMuxeR.exe") or which("tsmuxer") or which("tsMuxeR"))
        vlc = which("vlc.exe", VLC_DIRS) or which("vlc")
    else:
        ffmpeg = which("ffmpeg")
        ffprobe = which("ffprobe")
        makemkvcon64 = which("makemkvcon64")
        makemkvcon = which("makemkvcon")
        tsmuxer = str(local_tsmuxer) if local_tsmuxer else (which("tsmuxer") or which("tsMuxeR"))
        vlc = which("vlc")
    tools = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "makemkvcon64": makemkvcon64,
        "makemkvcon": makemkvcon,
        "tsmuxer": tsmuxer,
        "vlc": vlc,
    }
    tools["ffmpeg_encoders"] = []
    tools["hevc_encoders"] = []
    tools["hevc_nvenc"] = False
    if tools["ffmpeg"]:
        try:
            result = run_cmd([tools["ffmpeg"], "-hide_banner", "-encoders"], check=True, capture=True)
            encoder_names = sorted(set(re.findall(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)\s", result.stdout or "", re.MULTILINE)))
            tools["ffmpeg_encoders"] = encoder_names
            tools["hevc_encoders"] = [encoder for encoder in HEVC_ENCODERS if encoder in encoder_names]
            tools["hevc_nvenc"] = "hevc_nvenc" in encoder_names
        except ToolError:
            tools["hevc_nvenc"] = False
    return tools


def require_tool(tools: dict[str, Any], key: str) -> str:
    value = tools.get(key)
    if not value:
        raise ToolError(f"Required tool not found: {key}")
    return str(value)


def selected_hevc_encoder(args: Any) -> str:
    return str(getattr(args, "encoder", "hevc_nvenc") or "hevc_nvenc")


def encoder_is_hardware(encoder: str) -> bool:
    return encoder in HARDWARE_HEVC_ENCODERS


def require_hevc_encoder(tools: dict[str, Any], encoder: str) -> None:
    if encoder not in HEVC_ENCODERS:
        raise ToolError(f"Unsupported HEVC encoder: {encoder}")
    if encoder not in (tools.get("hevc_encoders") or []):
        available_encoders = tools.get("hevc_encoders") or []
        available = ", ".join(available_encoders) or "none"
        fallback = next(
            (candidate for candidate in ("libx265", "hevc_qsv", "hevc_amf") if candidate in available_encoders),
            None,
        )
        hint = f" Try rerunning with --encoder {fallback}." if fallback else ""
        raise ToolError(f"FFmpeg does not report requested HEVC encoder {encoder}. Available HEVC encoders: {available}.{hint}")


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    timeout_seconds: float | None = None,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    if verbose:
        print("+ " + format_cmd(cmd), flush=True)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=refreshed_env(),
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else sys.stderr,
            stderr=subprocess.PIPE if capture else sys.stderr,
            check=False,
            timeout=timeout_seconds,
            **hidden_process_kwargs(),
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(
            cmd,
            124,
            stdout=(exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout) or "",
            stderr=(exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr) or f"Timed out after {timeout_seconds} seconds",
        )
        if check:
            tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-40:])
            raise ToolError(f"Command timed out: {format_cmd(cmd)}\n{tail}")
        return result
    if check and result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-40:])
        raise ToolError(f"Command failed ({result.returncode}): {format_cmd(cmd)}\n{tail}")
    return result


def format_cmd(cmd: list[str]) -> str:
    parts = [str(part) for part in cmd]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def hidden_process_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}
