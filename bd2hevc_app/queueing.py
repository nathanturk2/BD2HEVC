"""Background job queue and status helpers."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .bitrate import safe_int
from .config import (
    ANIME_CQ_VALUE,
    DEFAULT_ANIME_CQ_MIN_DURATION,
    DEFAULT_AUDIO_MODE,
    DEFAULT_JOB_DIR,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_STEREO_AUDIO_BITRATE,
    DEFAULT_VLC_COMPATIBILITY_MODE,
    QUEUE_PAUSE_FILE,
    QUEUE_POLL_SECONDS,
    ROOT,
)
from .progress import WatchRenderer, progress_lines, read_text_flexible
from .tools import ToolError, format_cmd, refreshed_env, selected_hevc_encoder


def job_paths(job_id: str) -> dict[str, Path]:
    DEFAULT_JOB_DIR.mkdir(parents=True, exist_ok=True)
    base = DEFAULT_JOB_DIR / job_id
    return {
        "job": base.with_suffix(".job.json"),
        "plan": base.with_suffix(".plan.json"),
        "log": base.with_suffix(".log"),
        "report": base.with_suffix(".report.json"),
        "exitcode": base.with_suffix(".exitcode.txt"),
    }


def load_job(path: Path) -> dict[str, Any]:
    text = read_text_flexible(path)
    if not text.strip():
        raise json.JSONDecodeError("empty job file", text, 0)
    return json.loads(text)


def try_load_job(path: Path, *, attempts: int = 3, delay: float = 0.05) -> dict[str, Any] | None:
    for attempt in range(max(1, attempts)):
        try:
            return load_job(path)
        except (OSError, json.JSONDecodeError):
            if attempt + 1 < attempts:
                time.sleep(delay)
    return None


def save_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        last_error: OSError | None = None
        for attempt in range(20):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError as exc:
                last_error = exc
                if attempt + 1 >= 20:
                    raise
                time.sleep(0.05)
        else:
            if last_error:
                raise last_error
    finally:
        temp_path.unlink(missing_ok=True)


def known_job_files() -> list[Path]:
    if not DEFAULT_JOB_DIR.is_dir():
        return []
    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(DEFAULT_JOB_DIR.glob("*.job.json"), key=mtime, reverse=True)


def active_queue_jobs() -> list[tuple[Path, dict[str, Any], str]]:
    active: list[tuple[Path, dict[str, Any], str]] = []
    for path in known_job_files():
        job = try_load_job(path)
        if job is None:
            continue
        job["job_file"] = str(path)
        status = job_runtime_status(job)
        if status in {"running", "queued", "paused"}:
            active.append((path, job, status))
    return active


def find_job(identifier: str | None) -> tuple[Path, dict[str, Any]]:
    jobs = [(path, job) for path in known_job_files() if (job := try_load_job(path)) is not None]
    if not jobs:
        raise ToolError("No BD2HEVC background jobs found. Start one with: python bd2hevc.py start <source>")
    if not identifier:
        enriched = active_queue_jobs()
        if not enriched:
            enriched = []
            for path, job in jobs:
                job["job_file"] = str(path)
                enriched.append((path, job, job_runtime_status(job)))
        for path, job, status in enriched:
            if status == "running":
                return path, job
        for path, job, status in enriched:
            if status == "queued":
                return path, job
        for path, job, status in enriched:
            if status == "paused":
                return path, job
        return enriched[0][0], enriched[0][1]
    ident = identifier.lower()
    ident_path = Path(identifier).resolve() if any(ch in identifier for ch in ("/", "\\", ":")) else None
    for path, job in jobs:
        candidates = [
            job.get("id"),
            Path(str(job.get("source", ""))).name,
            Path(str(job.get("output", ""))).name,
            str(job.get("source", "")),
            str(job.get("output", "")),
            str(path),
        ]
        if any(str(candidate).lower() == ident for candidate in candidates if candidate):
            job["job_file"] = str(path)
            return path, job
        if ident_path and any(str(candidate) and Path(str(candidate)).resolve() == ident_path for candidate in (job.get("source"), job.get("output"), str(path))):
            job["job_file"] = str(path)
            return path, job
    raise ToolError(f"No background job matched: {identifier}")


def job_exit_code(job: dict[str, Any]) -> int | None:
    exit_value = job.get("exitcode")
    if not exit_value:
        return None
    exit_path = Path(str(exit_value))
    if not exit_path.exists():
        return None
    return safe_int(read_text_flexible(exit_path).strip())


def queue_is_paused() -> bool:
    return QUEUE_PAUSE_FILE.exists()


def process_creationflags(*, hidden: bool = True, detached: bool = False, new_group: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = 0
    if hidden:
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    if new_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def process_kwargs(*, hidden: bool = True, detached: bool = False, new_group: bool = False) -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": process_creationflags(hidden=hidden, detached=detached, new_group=new_group)}
    kwargs: dict[str, Any] = {}
    if detached or new_group:
        kwargs["start_new_session"] = True
    return kwargs


def terminate_process_tree(pid: int | None, *, force: bool = True) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            **process_kwargs(),
        )
        return result.returncode == 0
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def job_runtime_status(job: dict[str, Any]) -> str:
    if str(job.get("status") or "").lower() == "canceled":
        return "canceled"
    exit_code = job_exit_code(job)
    if exit_code == 0:
        return "completed"
    if exit_code == 130:
        return "canceled"
    if exit_code is not None:
        return f"failed ({exit_code})"
    if pid_is_running(safe_int(job.get("pid"))):
        status = str(job.get("status") or "running")
        if status == "queued":
            blockers = older_active_jobs(Path(str(job.get("job_file", ""))), job) if job.get("job_file") else []
            if not blockers:
                log_path = Path(str(job.get("log", "")))
                if log_path.exists() and " started at " in read_text_flexible(log_path):
                    return "running"
        return status
    return str(job.get("status") or "unknown")


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                **process_kwargs(),
            )
            return f'"{pid}"' in (result.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def append_option(argv: list[str], flag: str, value: Any, default: Any = None) -> None:
    if value is not None and value != default:
        argv.extend([flag, str(value)])


def flatten_cli_values(values: Any) -> list[str]:
    flattened: list[str] = []
    for value in values or []:
        if isinstance(value, (list, tuple)):
            flattened.extend(str(item) for item in value)
        else:
            flattened.append(str(value))
    return flattened


def flatten_cli_pairs(values: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for value in values or []:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            pairs.append((str(value[0]), str(value[1])))
    return pairs


def auto_command_for_job(args: argparse.Namespace, output: Path, report_path: Path, plan_path: Path | None = None) -> list[str]:
    script = ROOT / "bd2hevc.py"
    argv = [sys.executable, str(script), "auto", str(Path(args.source).resolve()), str(output)]
    for flag in (
        "fast_bitrate",
        "force_encode",
        "force",
        "makemkv",
        "no_makemkv",
        "require_makemkv",
        "no_patch_navigation",
        "no_bdj_compatibility_patches",
        "no_encode_ahead",
        "verbose",
    ):
        if getattr(args, flag, False):
            argv.append("--" + flag.replace("_", "-"))
    append_option(argv, "--hevc-bit-depth", getattr(args, "hevc_bit_depth", 8), 8)
    append_option(argv, "--encoder", selected_hevc_encoder(args), "hevc_nvenc")
    append_option(argv, "--encode-ahead-depth", getattr(args, "encode_ahead_depth", 3), 3)
    if getattr(args, "bitrate_preset_file", None):
        argv.extend(["--bitrate-preset-file", str(Path(args.bitrate_preset_file).resolve())])
    append_option(argv, "--quality", getattr(args, "quality", None))
    append_option(argv, "--bitrate-mode", getattr(args, "bitrate_mode", "balanced"), "balanced")
    append_option(argv, "--hevc-bitrate-factor", getattr(args, "hevc_bitrate_factor", None))
    append_option(argv, "--min-video-bitrate", getattr(args, "min_video_bitrate", 2_000_000), 2_000_000)
    append_option(argv, "--max-video-bitrate", getattr(args, "max_video_bitrate", 80_000_000), 80_000_000)
    append_option(argv, "--maxrate-multiplier", getattr(args, "maxrate_multiplier", 1.55), 1.55)
    append_option(argv, "--bufsize-multiplier", getattr(args, "bufsize_multiplier", 2.0), 2.0)
    append_option(argv, "--compact-cq-value", getattr(args, "compact_cq_value", ANIME_CQ_VALUE), ANIME_CQ_VALUE)
    append_option(argv, "--compact-cq-min-duration", getattr(args, "anime_cq_min_duration", DEFAULT_ANIME_CQ_MIN_DURATION), DEFAULT_ANIME_CQ_MIN_DURATION)
    append_option(argv, "--main-title-quality", getattr(args, "main_title_quality", None))
    append_option(argv, "--main-title-bitrate-mode", getattr(args, "main_title_bitrate_mode", None))
    append_option(argv, "--main-title-cq", getattr(args, "main_title_cq", None))
    top_n_quality = getattr(args, "top_n_quality", None)
    if top_n_quality:
        argv.extend(["--top-n-quality", str(top_n_quality[0]), str(top_n_quality[1])])
    top_n_mode = getattr(args, "top_n_bitrate_mode", None)
    if top_n_mode:
        argv.extend(["--top-n-bitrate-mode", str(top_n_mode[0]), str(top_n_mode[1])])
    top_n_cq = getattr(args, "top_n_cq", None)
    if top_n_cq:
        argv.extend(["--top-n-cq", str(top_n_cq[0]), str(top_n_cq[1])])
    for clip, quality in flatten_cli_pairs(getattr(args, "clip_quality", None)):
        argv.extend(["--clip-quality", clip, quality])
    for clip, mode in flatten_cli_pairs(getattr(args, "clip_bitrate_mode", None)):
        argv.extend(["--clip-bitrate-mode", clip, mode])
    for clip, cq_value in flatten_cli_pairs(getattr(args, "clip_cq", None)):
        argv.extend(["--clip-cq", clip, cq_value])
    copy_clips = flatten_cli_values(getattr(args, "copy_clips", None))
    if copy_clips:
        argv.extend(["--copy-clips", *copy_clips])
    append_option(argv, "--audio-mode", getattr(args, "audio_mode", DEFAULT_AUDIO_MODE), DEFAULT_AUDIO_MODE)
    append_option(argv, "--stereo-audio-bitrate", getattr(args, "stereo_audio_bitrate", DEFAULT_STEREO_AUDIO_BITRATE), DEFAULT_STEREO_AUDIO_BITRATE)
    append_option(argv, "--mono-audio-bitrate", getattr(args, "mono_audio_bitrate", DEFAULT_MONO_AUDIO_BITRATE), DEFAULT_MONO_AUDIO_BITRATE)
    append_option(argv, "--vlc-compat", getattr(args, "vlc_compat", DEFAULT_VLC_COMPATIBILITY_MODE), DEFAULT_VLC_COMPATIBILITY_MODE)
    for fix in getattr(args, "vlc_fix", None) or []:
        argv.extend(["--vlc-fix", str(fix)])
    for patch_file in getattr(args, "compat_patch_file", None) or []:
        argv.extend(["--compat-patch-file", str(patch_file)])
    if getattr(args, "decode_sample", 30.0) is None:
        argv.extend(["--decode-sample", "0"])
    else:
        append_option(argv, "--decode-sample", getattr(args, "decode_sample", 30.0), 30.0)
    if plan_path is not None:
        argv.extend(["--progress-plan", str(plan_path)])
    argv.extend(["--no-progress", "--report", str(report_path)])
    return argv


def start_background_process(job_path: Path) -> int:
    script = ROOT / "bd2hevc.py"
    cmd = [sys.executable, str(script), "run-job", str(job_path)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=refreshed_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **process_kwargs(detached=True, new_group=True),
    )
    return int(proc.pid)


def older_active_jobs(current_path: Path, current_job: dict[str, Any]) -> list[dict[str, Any]]:
    current_order = float(current_job.get("queue_order") or 0)
    active: list[dict[str, Any]] = []
    for path in known_job_files():
        if path.resolve() == current_path.resolve():
            continue
        job = try_load_job(path)
        if job is None:
            continue
        if float(job.get("queue_order") or 0) >= current_order:
            continue
        if str(job.get("status") or "").lower() == "canceled":
            continue
        if job_exit_code(job) is not None:
            continue
        if pid_is_running(safe_int(job.get("pid"))):
            active.append(job)
    active.sort(key=lambda item: float(item.get("queue_order") or 0))
    return active


def job_display_id(job: dict[str, Any]) -> str:
    return str(job.get("id") or Path(str(job.get("output") or "")).name or Path(str(job.get("source") or "")).name or "(unknown job)")


def queue_waiting_lines(job: dict[str, Any]) -> list[str]:
    job_file = str(job.get("job_file") or "")
    blockers: list[dict[str, Any]] = []
    if job_file:
        try:
            blockers = older_active_jobs(Path(job_file), job)
        except (OSError, ValueError):
            blockers = []
    if blockers:
        count = len(blockers)
        noun = "job" if count == 1 else "jobs"
        lines = [f"Queue: {count} {noun} ahead"]
        lines.append(f"Next ahead: {job_display_id(blockers[0])}")
        if count > 1:
            also_ahead = ", ".join(job_display_id(blocker) for blocker in blockers[1:4])
            if count > 4:
                also_ahead += f", +{count - 4} more"
            lines.append(f"Also ahead: {also_ahead}")
        return lines
    waiting_for = job.get("waiting_for")
    if waiting_for:
        return [f"Next ahead: {waiting_for}"]
    return []


def wait_for_queue_turn(job_path: Path, job: dict[str, Any], log: Any) -> dict[str, Any]:
    while True:
        try:
            job = load_job(job_path)
        except Exception:
            job["status"] = "canceled"
            return job
        if job.get("cancel_requested") or str(job.get("status") or "").lower() == "canceled":
            job["status"] = "canceled"
            job["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_job(job_path, job)
            log.write("Job canceled before conversion started.\n")
            log.flush()
            return job
        if queue_is_paused():
            job["status"] = "paused"
            job["waiting_for"] = "queue pause"
            save_job(job_path, job)
            log.write("Waiting because the BD2HEVC queue is paused.\n")
            log.flush()
            time.sleep(QUEUE_POLL_SECONDS)
            continue
        blockers = older_active_jobs(job_path, job)
        if not blockers:
            job["status"] = "running"
            job["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            job.pop("waiting_for", None)
            save_job(job_path, job)
            return job
        waiting_for = blockers[0].get("id")
        job["status"] = "queued"
        job["waiting_for"] = waiting_for
        save_job(job_path, job)
        log.write(f"Waiting for earlier job: {waiting_for}\n")
        log.flush()
        time.sleep(QUEUE_POLL_SECONDS)


def cmd_run_job(args: argparse.Namespace) -> int:
    job_path = Path(args.job).resolve()
    job = try_load_job(job_path)
    if job is None:
        raise ToolError(f"Could not read job file: {job_path}")
    log_path = Path(job["log"])
    exit_path = Path(job["exitcode"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    job["pid"] = os.getpid()
    job["status"] = "queued"
    save_job(job_path, job)
    returncode = 1
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"BD2HEVC job {job['id']} queued at {job.get('queued_at') or job.get('created_at')}\n")
            job = wait_for_queue_turn(job_path, job, log)
            if str(job.get("status") or "").lower() == "canceled":
                returncode = 130
            else:
                log.write(f"BD2HEVC job {job['id']} started at {job['started_at']}\n")
                log.write("Command: " + format_cmd(job["command"]) + "\n\n")
                log.flush()
                proc = subprocess.run(
                    job["command"],
                    cwd=str(ROOT),
                    env=refreshed_env(),
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **process_kwargs(),
                )
                returncode = int(proc.returncode)
                log.write(f"\nBD2HEVC job finished with exit code {returncode}\n")
    except Exception as exc:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\nBD2HEVC job failed before completion: {exc}\n")
        returncode = 1
    exit_path.write_text(str(returncode), encoding="utf-8")
    job["status"] = "completed" if returncode == 0 else ("canceled" if returncode == 130 else "failed")
    job["exitcode"] = str(exit_path)
    job["returncode"] = returncode
    job["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_job(job_path, job)
    return returncode


def job_status_lines(job: dict[str, Any], *, width: int) -> list[str]:
    output_name = Path(str(job.get("output") or "")).name
    job_id = str(job.get("id") or "")
    title = output_name or Path(str(job.get("source") or "")).name or job_id
    lines = [f"Disc: {title}"]
    if job_id:
        lines.append(f"Job: {job_id}")
    plan_path = Path(str(job.get("plan") or ""))
    if plan_path.is_file() and plan_path.stat().st_size > 0:
        args = argparse.Namespace(
            target=job["output"],
            plan=job["plan"],
            log=job.get("log"),
            width=width,
            watch=0,
        )
        lines.extend(progress_lines(args, inspect_outputs=False))
    else:
        bar_width = min(width, 32)
        lines.append(f"[{'-' * bar_width}]   planning scan has not finished yet")
        lines.append("encoded clips: unknown until planning completes")
    exit_path = Path(job["exitcode"])
    exit_code = safe_int(read_text_flexible(exit_path).strip()) if exit_path.exists() else None
    status = job_runtime_status(job)
    if exit_code == 0:
        lines.append("Status: completed")
        lines.append(f"Output: {job['output']}")
        if Path(job.get("report", "")).exists():
            lines.append(f"Full report: {job['report']}")
    elif exit_code is not None:
        lines.append(f"Status: failed (exit code {exit_code})")
        lines.append(f"Log: {job.get('log')}")
    else:
        lines.append(f"Status: {status}")
        if status == "queued":
            lines.extend(queue_waiting_lines(job))
        lines.append(f"Log: {job.get('log')}")
    return lines


def print_job_status(job: dict[str, Any], *, width: int) -> None:
    print("\n".join(job_status_lines(job, width=width)))


def cmd_status(args: argparse.Namespace) -> int:
    explicit_job = bool(args.job)
    _, job = find_job(args.job)
    renderer = WatchRenderer() if args.watch else None
    try:
        while True:
            if args.watch and not explicit_job:
                _, job = find_job(None)
            lines = job_status_lines(job, width=args.width)
            if renderer:
                renderer.render(lines)
            else:
                print("\n".join(lines))
            if not args.watch:
                return 0
            exit_path = Path(job["exitcode"])
            if explicit_job and exit_path.exists():
                return safe_int(read_text_flexible(exit_path).strip()) or 0
            if not explicit_job and not active_queue_jobs():
                return (safe_int(read_text_flexible(exit_path).strip()) or 0) if exit_path.exists() else 0
            time.sleep(args.watch)
            if explicit_job:
                _, job = find_job(job.get("id"))
    finally:
        if renderer:
            renderer.close()


def cmd_jobs(args: argparse.Namespace) -> int:
    jobs = [(path, job) for path in known_job_files() if (job := try_load_job(path)) is not None]
    if not jobs:
        print("No BD2HEVC background jobs found.")
        return 0

    def sort_key(item: tuple[Path, dict[str, Any]]) -> tuple[int, float]:
        path, job = item
        job["job_file"] = str(path)
        status = job_runtime_status(job)
        if status == "running":
            return (0, float(job.get("queue_order") or 0))
        if status in {"queued", "paused"}:
            return (1, float(job.get("queue_order") or 0))
        return (2, -path.stat().st_mtime)

    def status_category(status: str) -> str:
        if status in {"running", "queued", "paused"}:
            return "active"
        if status.startswith("failed"):
            return "failed"
        if status == "completed":
            return "completed"
        if status == "canceled":
            return "canceled"
        return "other"

    selected_categories = {
        name
        for name, selected in (
            ("active", getattr(args, "active", False)),
            ("failed", getattr(args, "failed", False)),
            ("completed", getattr(args, "completed", False)),
            ("canceled", getattr(args, "canceled", False)),
        )
        if selected
    }
    enriched: list[tuple[Path, dict[str, Any], str, str]] = []
    for path, job in jobs:
        job["job_file"] = str(path)
        status = job_runtime_status(job)
        enriched.append((path, job, status, status_category(status)))

    if getattr(args, "hide_old_failed", False):
        latest_completed_by_output: dict[str, float] = {}
        for path, job, _status, category in enriched:
            if category != "completed":
                continue
            output = str(job.get("output") or "")
            if not output:
                continue
            order = float(job.get("queue_order") or path.stat().st_mtime)
            latest_completed_by_output[output] = max(order, latest_completed_by_output.get(output, 0.0))
        filtered: list[tuple[Path, dict[str, Any], str, str]] = []
        for path, job, status, category in enriched:
            output = str(job.get("output") or "")
            order = float(job.get("queue_order") or path.stat().st_mtime)
            if category == "failed" and output and latest_completed_by_output.get(output, 0.0) > order:
                continue
            filtered.append((path, job, status, category))
        enriched = filtered

    if selected_categories:
        enriched = [item for item in enriched if item[3] in selected_categories]

    if not enriched:
        print("No matching BD2HEVC jobs found.")
        return 0

    if queue_is_paused():
        print(f"Queue: paused ({QUEUE_PAUSE_FILE})")
    for path, job, status, _category in sorted(enriched, key=lambda item: sort_key((item[0], item[1])))[: args.limit]:
        print(f"{job.get('id')}  {status}")
        if status == "queued":
            for line in queue_waiting_lines(job):
                print(f"  {line}")
        if status == "paused":
            print("  waiting for: queue resume")
        print(f"  output: {job.get('output')}")
    return 0


def cmd_pause_queue(args: argparse.Namespace) -> int:
    DEFAULT_JOB_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_PAUSE_FILE.write_text(
        json.dumps({"paused_at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": args.reason}, indent=2),
        encoding="utf-8",
    )
    print("BD2HEVC queue paused. The current running job continues; queued jobs will wait.")
    print("Resume with: python bd2hevc.py resume-queue")
    return 0


def cmd_resume_queue(args: argparse.Namespace) -> int:
    if QUEUE_PAUSE_FILE.exists():
        QUEUE_PAUSE_FILE.unlink()
        print("BD2HEVC queue resumed.")
    else:
        print("BD2HEVC queue was not paused.")
    return 0


def mark_job_canceled(job_path: Path, job: dict[str, Any], *, kill: bool) -> bool:
    status = job_runtime_status(job)
    running = status == "running"
    if running and not kill:
        raise ToolError("That job is already running. Use --kill to stop a running conversion.")
    job["cancel_requested"] = True
    job["status"] = "canceled" if not running else "canceling"
    job["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_job(job_path, job)
    terminated = False
    if pid_is_running(safe_int(job.get("pid"))) and (kill or status in {"queued", "paused"}):
        terminated = terminate_process_tree(safe_int(job.get("pid")))
    exit_path = Path(str(job.get("exitcode", "")))
    if exit_path:
        exit_path.parent.mkdir(parents=True, exist_ok=True)
        exit_path.write_text("130", encoding="utf-8")
    job["status"] = "canceled"
    job["returncode"] = 130
    save_job(job_path, job)
    return terminated


def cmd_cancel_job(args: argparse.Namespace) -> int:
    job_path, job = find_job(args.job)
    status = job_runtime_status(job)
    terminated = mark_job_canceled(job_path, job, kill=args.kill)
    print(f"Canceled job: {job.get('id')}")
    if status == "running":
        print("Stopped running process tree." if terminated else "Cancel was recorded, but no process tree was found to stop.")
    return 0


def cmd_remove_job(args: argparse.Namespace) -> int:
    job_path, job = find_job(args.job)
    status = job_runtime_status(job)
    if status in {"running", "queued", "paused"}:
        mark_job_canceled(job_path, job, kill=args.kill or status in {"queued", "paused"})
    if job_path.exists():
        job_path.unlink()
    print(f"Removed job record: {job.get('id')}")
    print("Logs, reports, plans, and converted output were left in place.")
    return 0
