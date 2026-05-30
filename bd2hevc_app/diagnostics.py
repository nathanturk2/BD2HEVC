"""Redacted diagnostic bundle creation for user support reports."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import DEFAULT_REPORT_DIR, ROOT, VERSION
from .progress import read_text_flexible
from .queueing import find_job, job_runtime_status
from .scan import find_disc_roots
from .tools import ToolError, discover_tools, format_cmd, hidden_process_kwargs, refreshed_env


TEXT_REPORT_SUFFIXES = {".json", ".log", ".txt"}
RAW_DISC_SUFFIXES = {".m2ts", ".ssif", ".jar", ".bdjo", ".mobj", ".bdmv", ".clpi", ".mpls"}
DEFAULT_DIAGNOSTIC_LOG_LINES = 5000
LOG_HIGHLIGHT_PATTERN = re.compile(
    r"(error|failed|failure|exception|traceback|critical|invalid|unsupported|could not|cannot|no such|jsondecodeerror|toolerror)",
    re.IGNORECASE,
)


def safe_report_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or "diagnostic"


def path_variants(path: Path) -> list[str]:
    values = {str(path)}
    try:
        resolved = path.resolve()
        values.add(str(resolved))
        values.add(resolved.as_posix())
    except OSError:
        pass
    values.add(path.as_posix())
    return sorted((value for value in values if value), key=len, reverse=True)


def redaction_map(paths: list[Path]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    entries = [(ROOT, "<bd2hevc-root>"), (Path.home(), "<home>")]
    entries.extend((path, f"<path-{index}>") for index, path in enumerate(paths, start=1) if path)
    for path, replacement in entries:
        for value in path_variants(path):
            mapping.setdefault(value, replacement)
    return dict(sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True))


def redact_text(text: str, mapping: dict[str, str]) -> str:
    redacted = text
    for original, replacement in mapping.items():
        if not original:
            continue
        redacted = redacted.replace(original, replacement)
        if os.name == "nt":
            redacted = re.sub(re.escape(original), replacement, redacted, flags=re.IGNORECASE)
    return redacted


def redact_value(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, mapping)
    if isinstance(value, list):
        return [redact_value(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, mapping) for key, item in value.items()}
    return value


def file_info(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": stat.st_size,
        "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        "suffix": path.suffix.lower(),
        "raw_disc_file": path.suffix.lower() in RAW_DISC_SUFFIXES,
    }


def summarize_disc_tree(path: Path) -> dict[str, Any]:
    root = path.resolve()
    roots = find_disc_roots([root])
    disc_root = roots[0] if roots else root
    files: list[dict[str, Any]] = []
    counts_by_suffix: dict[str, int] = {}
    total_bytes_by_suffix: dict[str, int] = {}
    total_bytes = 0
    if disc_root.is_file():
        try:
            stat = disc_root.stat()
            info = {
                "path": disc_root.name,
                "size_bytes": stat.st_size,
                "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
                "suffix": disc_root.suffix.lower(),
                "raw_disc_file": disc_root.suffix.lower() in RAW_DISC_SUFFIXES,
            }
            files.append(info)
            counts_by_suffix[disc_root.suffix.lower()] = 1
            total_bytes_by_suffix[disc_root.suffix.lower()] = stat.st_size
            total_bytes = stat.st_size
        except OSError:
            pass
    elif disc_root.is_dir():
        for item in sorted(disc_root.rglob("*")):
            if not item.is_file():
                continue
            try:
                info = file_info(item, disc_root)
            except OSError:
                continue
            files.append(info)
            suffix = str(info["suffix"])
            counts_by_suffix[suffix] = counts_by_suffix.get(suffix, 0) + 1
            total_bytes_by_suffix[suffix] = total_bytes_by_suffix.get(suffix, 0) + int(info["size_bytes"])
            total_bytes += int(info["size_bytes"])
    return {
        "exists": root.exists(),
        "input_name": root.name,
        "disc_root_name": disc_root.name,
        "bdmv_found": bool(roots),
        "total_files": len(files),
        "total_bytes": total_bytes,
        "counts_by_suffix": counts_by_suffix,
        "total_bytes_by_suffix": total_bytes_by_suffix,
        "files": files,
    }


def tail_text(path: Path, lines: int) -> str:
    text = read_text_flexible(path)
    if lines <= 0:
        return text
    split = text.splitlines()
    return "\n".join(split[-lines:])


def tool_version(executable: str | None, args: list[str], timeout_seconds: float = 8.0) -> dict[str, Any]:
    if not executable:
        return {"found": False}
    cmd = [executable, *args]
    try:
        with tempfile.TemporaryDirectory(prefix="bd2hevc-tool-version-") as workdir:
            result = subprocess.run(
                cmd,
                cwd=workdir,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
                env=refreshed_env(),
                **hidden_process_kwargs(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"found": True, "executable": executable, "error": str(exc)}
    output = (result.stdout or result.stderr or "").splitlines()
    return {
        "found": True,
        "executable": executable,
        "returncode": result.returncode,
        "first_line": output[0] if output else "",
    }


def collect_tool_summary() -> dict[str, Any]:
    tools = discover_tools()
    return {
        "paths": tools,
        "versions": {
            "ffmpeg": tool_version(tools.get("ffmpeg"), ["-version"]),
            "ffprobe": tool_version(tools.get("ffprobe"), ["-version"]),
            "tsmuxer": tool_version(tools.get("tsmuxer"), ["--version"]),
            "vlc": tool_version(tools.get("vlc"), ["--version"]),
            "makemkvcon": tool_version(tools.get("makemkvcon64") or tools.get("makemkvcon"), ["--version"]),
        },
    }


def run_light_validation(target: Path, source: Path | None, mapping: dict[str, str]) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "bd2hevc.py"),
        "validate",
        str(target),
        "--decode-sample",
        "0",
        "--no-makemkv",
        "--json",
    ]
    if source:
        cmd.extend(["--reference", str(source)])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=900,
            env=refreshed_env(),
            **hidden_process_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timeout": True,
            "command": redact_text(format_cmd(cmd), mapping),
            "stdout": redact_text((exc.stdout or "") if isinstance(exc.stdout, str) else "", mapping),
            "stderr": redact_text((exc.stderr or "") if isinstance(exc.stderr, str) else "", mapping),
        }
    payload: Any = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "command": redact_text(format_cmd(cmd), mapping),
        "report": redact_value(payload, mapping) if payload is not None else None,
        "stdout_tail": redact_text("\n".join((result.stdout or "").splitlines()[-80:]), mapping),
        "stderr_tail": redact_text("\n".join((result.stderr or "").splitlines()[-80:]), mapping),
    }


def copy_redacted_report(path: Path, destination: Path, mapping: dict[str, str], *, max_lines: int = 0) -> bool:
    if not path.exists() or path.suffix.lower() not in TEXT_REPORT_SUFFIXES:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = tail_text(path, max_lines) if max_lines else read_text_flexible(path)
    destination.write_text(redact_text(text, mapping), encoding="utf-8")
    return True


def write_log_highlights(path: Path, destination: Path, mapping: dict[str, str], *, max_matches: int = 600) -> bool:
    if not path.exists():
        return False
    matches: list[str] = []
    for line in read_text_flexible(path).splitlines():
        stripped = line.strip()
        if not stripped or "BD2HEVC_PROGRESS" in stripped:
            continue
        if LOG_HIGHLIGHT_PATTERN.search(stripped):
            matches.append(stripped[-2000:])
    if not matches:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    kept = matches[-max_matches:]
    omitted = len(matches) - len(kept)
    header = []
    if omitted:
        header.append(f"Earlier matching lines omitted: {omitted}")
    header.append(f"Matching lines included: {len(kept)}")
    destination.write_text(redact_text("\n".join(header + ["", *kept]) + "\n", mapping), encoding="utf-8")
    return True


def write_readme(destination: Path) -> None:
    destination.write_text(
        "\n".join(
            [
                "BD2HEVC diagnostic bundle",
                "",
                "Attach this zip to a GitHub issue when a converted backup behaves differently from the source.",
                "",
                "This bundle is designed to avoid media and disc assets. It contains redacted logs, generated reports,",
                "tool/version summaries, validation output, and a file manifest with names and sizes only.",
                "",
                "Please do not add .m2ts media files, JARs, keys, decryption logs, or raw disc assets to the issue.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def create_diagnostic_bundle(
    target: Path,
    *,
    source: Path | None = None,
    job_identifier: str | None = None,
    output: Path | None = None,
    log_lines: int = DEFAULT_DIAGNOSTIC_LOG_LINES,
    run_validation: bool = True,
    zip_output: bool = True,
) -> dict[str, Any]:
    target = target.resolve()
    source = source.resolve() if source else None
    job_path: Path | None = None
    job: dict[str, Any] | None = None
    if job_identifier or target.exists():
        try:
            job_path, job = find_job(job_identifier or str(target))
        except ToolError:
            job_path, job = None, None
    sensitive_paths = [target]
    if source:
        sensitive_paths.append(source)
    if job_path:
        sensitive_paths.append(job_path)
    if job:
        for key in ("source", "output", "plan", "log", "report", "exitcode"):
            value = job.get(key)
            if value:
                sensitive_paths.append(Path(str(value)))
    mapping = redaction_map(sensitive_paths)

    base_name = safe_report_name(target.name or (job_identifier or "diagnostic"))
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    diagnostics_dir = DEFAULT_REPORT_DIR / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    if output is None:
        output = diagnostics_dir / f"{base_name}-{timestamp}.zip" if zip_output else diagnostics_dir / f"{base_name}-{timestamp}"
    output = output.resolve()

    with tempfile.TemporaryDirectory(prefix="bd2hevc-diagnostic-") as temp:
        workdir = Path(temp) / f"{base_name}-{timestamp}"
        workdir.mkdir(parents=True)
        write_readme(workdir / "README.txt")

        environment = {
            "bd2hevc_version": VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "platform": platform.platform(),
            "system": platform.system(),
            "python": sys.version,
            "cwd": redact_text(str(Path.cwd()), mapping),
        }
        diagnostic: dict[str, Any] = {
            "environment": environment,
            "target": redact_value(summarize_disc_tree(target), mapping),
            "source": redact_value(summarize_disc_tree(source), mapping) if source else None,
            "job": None,
            "tools": redact_value(collect_tool_summary(), mapping),
            "validation": None,
        }
        if job:
            diagnostic["job"] = {
                "file": redact_text(str(job_path), mapping) if job_path else None,
                "status": job_runtime_status(job),
                "job_json": redact_value(job, mapping),
            }
            report_dir = workdir / "reports"
            if job_path:
                copy_redacted_report(job_path, report_dir / "job.job.json", mapping)
            for key, filename in (("plan", "job.plan.json"), ("report", "job.report.json"), ("exitcode", "job.exitcode.txt")):
                value = job.get(key)
                if value:
                    copy_redacted_report(Path(str(value)), report_dir / filename, mapping)
            log_value = job.get("log")
            if log_value:
                log_path = Path(str(log_value))
                copy_redacted_report(log_path, report_dir / "job.log.tail.txt", mapping, max_lines=log_lines)
                write_log_highlights(log_path, report_dir / "job.log.error-highlights.txt", mapping)
        if run_validation and target.exists():
            diagnostic["validation"] = run_light_validation(target, source, mapping)

        (workdir / "diagnostic.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
        if zip_output:
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                output.unlink()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(workdir.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(workdir).as_posix())
            bundle_path = output
        else:
            if output.exists():
                if output.is_dir():
                    shutil.rmtree(output)
                else:
                    output.unlink()
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workdir, output)
            bundle_path = output

    return {
        "ok": True,
        "bundle": str(bundle_path),
        "zipped": zip_output,
        "validation_run": run_validation and target.exists(),
        "job_found": job is not None,
    }


def cmd_diagnose(args: argparse.Namespace) -> int:
    result = create_diagnostic_bundle(
        Path(args.target),
        source=Path(args.source) if args.source else None,
        job_identifier=args.job,
        output=Path(args.output) if args.output else None,
        log_lines=args.log_lines,
        run_validation=not args.no_validation,
        zip_output=not args.no_zip,
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print("BD2HEVC diagnostic bundle created.")
        print(f"Saved to: {result['bundle']}")
        if result["validation_run"]:
            print("Included: lightweight validation report")
        if result["job_found"]:
            print("Included: matching job metadata and redacted log tail")
        print("Attach this bundle to a GitHub issue; do not attach media files or disc assets.")
    return 0
