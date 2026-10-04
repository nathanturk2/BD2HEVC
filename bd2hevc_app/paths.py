"""Immutable installed resources and writable per-user state."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

APP = 'BD2HEVC'
PREFIX = APP.upper()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_packaged = Path(str(files('bd2hevc_app').joinpath("resources")))
ROOT = _packaged if _packaged.is_dir() else PROJECT_ROOT

def state_root() -> Path:
    override = os.environ.get(PREFIX + "_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP.lower()

def reports_root() -> Path:
    destination = state_root() / "reports"
    legacy = PROJECT_ROOT / "reports"
    if os.environ.get(PREFIX + "_STATE_DIR") or not legacy.is_dir() or ROOT != PROJECT_ROOT:
        return destination
    # A running legacy worker still owns its original state. Migrate on a later launch.
    for record in (*legacy.rglob("*.json"), *legacy.rglob("*.lock")):
        try:
            job = json.loads(record.read_text(encoding="utf-8-sig"))
            if not isinstance(job, dict): continue
            pid = job.get("runner_pid") or job.get("watcher_pid") or job.get("pid")
            active = job.get("status") in {"running", "queued", "paused", "active"} or record.suffix == ".lock"
            if active and pid:
                if os.name == "nt":
                    result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if f'"{int(pid)}"' in result.stdout: return legacy
                else:
                    os.kill(int(pid), 0)
                    return legacy
        except (OSError, ValueError, TypeError):
            pass
    marker = state_root() / "legacy-state-migrated.json"
    if not marker.exists():
        def relocate(value):
            if isinstance(value, str): return value.replace(str(legacy), str(destination))
            if isinstance(value, list): return [relocate(item) for item in value]
            if isinstance(value, dict): return {key: relocate(item) for key,item in value.items()}
            return value
        for source in legacy.rglob("*"):
            if not source.is_file() or source.is_symlink() or source.suffix.lower() not in {".json", ".log", ".txt", ".paused"}:
                continue
            target = destination / source.relative_to(legacy)
            if target.exists(): continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".json":
                try:
                    value = relocate(json.loads(source.read_text(encoding="utf-8-sig")))
                    target.write_text(json.dumps(value, indent=2), encoding="utf-8")
                    continue
                except (OSError, ValueError): pass
            shutil.copy2(source, target)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"source":str(legacy),"destination":str(destination)}),encoding="utf-8")
    return destination

STATE_ROOT = state_root()
REPORT_ROOT = reports_root()
TOOL_ROOT = STATE_ROOT / "tools"
