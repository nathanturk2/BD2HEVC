"""Progress log parsing, watch rendering, and progress display helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .bitrate import format_duration, parse_timecode, safe_float, safe_int
from .scan import inspect_clip
from .tools import ToolError, discover_tools


def progress_event(event: str, clip_file: str, **fields: Any) -> None:
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = f" {extras}" if extras else ""
    print(f"BD2HEVC_PROGRESS {event} {clip_file}{suffix}", file=sys.stderr, flush=True)


def normalize_clip_name(clip: str) -> str:
    return clip if clip.lower().endswith(".m2ts") else f"{clip}.m2ts"


def output_matches_hevc_bit_depth(info: dict[str, Any], hevc_bit_depth: int) -> bool:
    video = info.get("video") or {}
    if video.get("codec_name") != "hevc":
        return False
    profile = str(video.get("profile") or "").lower()
    pix_fmt = str(video.get("pix_fmt") or "").lower()
    if hevc_bit_depth == 8:
        return pix_fmt == "yuv420p" and "10" not in profile
    if hevc_bit_depth == 10:
        return pix_fmt in {"yuv420p10le", "p010le"} and "10" in profile
    raise ToolError(f"Unsupported HEVC bit depth: {hevc_bit_depth}")


def load_progress_plan(plan_path: Path) -> list[dict[str, Any]]:
    if not plan_path.exists():
        raise ToolError(f"Progress plan not found: {plan_path}")
    try:
        payload = json.loads(read_text_flexible(plan_path))
    except json.JSONDecodeError as exc:
        raise ToolError(f"Progress plan is not valid JSON: {plan_path}") from exc
    clips = payload.get("reencode_clips") or payload.get("selected") or []
    normalized = []
    for clip in clips:
        filename = clip.get("file") or clip.get("clip")
        if filename and clip.get("duration"):
            normalized.append({**clip, "file": normalize_clip_name(filename)})
    return normalized


def read_text_flexible(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def latest_log_progress(log_path: Path) -> dict[str, Any]:
    if not log_path.exists():
        return {}
    text = read_text_flexible(log_path)
    marker_pattern = re.compile(r"(?:^|[\r\n])BD2HEVC_PROGRESS[^\S\r\n]+(\S+)[^\S\r\n]+(\S+)(?:[^\S\r\n]+([^\r\n]*))?", re.MULTILINE)
    markers = list(marker_pattern.finditer(text))
    if markers:
        encode_starts: dict[str, re.Match[str]] = {}
        mux_starts: dict[str, re.Match[str]] = {}
        encode_done: set[str] = set()
        encoded_done: list[str] = []
        mux_done: set[str] = set()
        validate_done: list[str] = []
        pipeline_mode = None
        for marker in markers:
            event, clip_file, rest = marker.group(1), marker.group(2), marker.group(3) or ""
            if event == "pipeline":
                pipeline_mode = rest
            elif event == "encode-start":
                encode_starts[clip_file] = marker
                encode_done.discard(clip_file)
                encoded_done = [clip for clip in encoded_done if clip != clip_file]
            elif event in {"encode-done", "encode-failed"}:
                encode_done.add(clip_file)
                if event == "encode-done" and clip_file not in encoded_done:
                    encoded_done.append(clip_file)
            elif event == "mux-start":
                mux_starts[clip_file] = marker
                mux_done.discard(clip_file)
            elif event in {"mux-done", "mux-failed"}:
                mux_done.add(clip_file)
            elif event == "validate-done":
                validate_done.append(clip_file)
                mux_done.add(clip_file)
        active_encode = next((clip for clip, marker in sorted(encode_starts.items(), key=lambda item: item[1].start(), reverse=True) if clip not in encode_done), None)
        active_mux = next((clip for clip, marker in sorted(mux_starts.items(), key=lambda item: item[1].start(), reverse=True) if clip not in mux_done), None)

        def marker_segment(start_event: str, clip_file: str | None, done_events: set[str]) -> str:
            if not clip_file:
                return ""
            start = next((m for m in reversed(markers) if m.group(1) == start_event and m.group(2) == clip_file), None)
            if not start:
                return ""
            end = next((m for m in markers if m.start() > start.start() and m.group(2) == clip_file and m.group(1) in done_events), None)
            return text[start.end() : (end.start() if end else len(text))]

        encode_segment = marker_segment("encode-start", active_encode, {"encode-done", "encode-failed"})
        mux_segment = marker_segment("mux-start", active_mux, {"mux-done", "mux-failed"})
        encode_times = re.findall(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})", encode_segment)
        encode_seconds = parse_timecode(encode_times[-1]) if encode_times else None
        mux_matches = re.findall(r"(?m)^\s*(\d+(?:\.\d+)?)%\s+complete\s*$", mux_segment)
        mux_percent = safe_float(mux_matches[-1]) if mux_matches else None
        speed_matches = re.findall(r"speed=\s*([0-9.]+x)", encode_segment)
        encode_speed = speed_matches[-1] if speed_matches else None
        current_stage = "muxing + encoding" if active_mux and active_encode else ("remuxing" if active_mux else ("encoding" if active_encode else None))
        return {
            "current_file": active_mux or active_encode,
            "current_seconds": encode_seconds if not active_mux else None,
            "current_stage": current_stage,
            "current_speed": encode_speed,
            "mux_file": active_mux,
            "mux_percent": mux_percent,
            "encode_file": active_encode,
            "encode_seconds": encode_seconds,
            "encode_speed": encode_speed,
            "encoded_files": encoded_done,
            "done_files": list(dict.fromkeys(validate_done or sorted(mux_done))),
            "pipeline": pipeline_mode,
        }
    inputs = list(re.finditer(r"from '([^']+?([0-9]{5}\.m2ts))'", text))
    done_files: list[str] = []
    current_file = inputs[-1].group(2) if inputs else None
    current_segment = ""
    current_stage = None
    for index, match in enumerate(inputs):
        segment_end = inputs[index + 1].start() if index + 1 < len(inputs) else len(text)
        segment = text[match.end() : segment_end]
        if "Mux successful complete" in segment:
            done_files.append(match.group(2))
        if index == len(inputs) - 1:
            current_segment = segment
    times = re.findall(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})", current_segment or text)
    current_seconds = parse_timecode(times[-1]) if times else None
    mux_matches = re.findall(r"(?m)^\s*(\d+(?:\.\d+)?)%\s+complete\s*$", current_segment)
    mux_percent = safe_float(mux_matches[-1]) if mux_matches else None
    speed_matches = re.findall(r"speed=\s*([0-9.]+x)", current_segment)
    current_speed = speed_matches[-1] if speed_matches else None
    if mux_percent is not None and "Mux successful complete" not in current_segment:
        current_stage = "remuxing"
    elif current_seconds:
        current_stage = "encoding"
    if current_file and current_file in done_files:
        current_seconds = None
        mux_percent = None
        current_stage = None
    return {
        "current_file": current_file,
        "current_seconds": current_seconds,
        "current_stage": current_stage,
        "current_speed": current_speed,
        "mux_percent": mux_percent,
        "done_files": done_files,
    }


def progress_bar(percent: float, width: int) -> str:
    filled = max(0, min(width, int(round(width * percent / 100.0))))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def clamp_percent(percent: float) -> float:
    return max(0.0, min(100.0, percent))


def emit_conversion_progress(
    done_seconds: float,
    total_seconds: float,
    done_clips: int,
    total_clips: int,
    *,
    current: str | None,
    stage: str,
    enabled: bool,
    width: int = 32,
) -> None:
    if not enabled:
        return
    percent = (done_seconds / total_seconds * 100.0) if total_seconds else (done_clips / total_clips * 100.0 if total_clips else 0.0)
    bar = progress_bar(percent, width)
    suffix = f" current: {current}" if current else ""
    print(f"{bar} {percent:5.1f}%  {done_clips}/{total_clips} clips  stage: {stage}{suffix}", file=sys.stderr, flush=True)


def progress_lines(args: argparse.Namespace, *, inspect_outputs: bool = True) -> list[str]:
    target = Path(args.target).resolve()
    plan_path = Path(args.plan).resolve()
    log_path = Path(args.log).resolve() if args.log else None
    clips = load_progress_plan(plan_path)
    plan_payload = json.loads(read_text_flexible(plan_path))
    desired_bit_depth = safe_int(plan_payload.get("hevc_bit_depth"))
    exit_code: int | None = None
    if log_path:
        exit_path = log_path.with_name(log_path.stem + ".exitcode.txt")
        if exit_path.exists():
            exit_code = safe_int(read_text_flexible(exit_path).strip())
    total_seconds = sum(float(clip.get("duration") or 0) for clip in clips)
    lines: list[str] = []
    if exit_code == 0:
        lines.append(f"{progress_bar(100.0, args.width)} {100.0:5.1f}% encoded  {len(clips)}/{len(clips)} clips complete  stage: completed")
        return lines
    if exit_code is not None:
        lines.append(f"{progress_bar(0.0, args.width)}   0.0% encoded  0/{len(clips)} clips complete  stage: failed exitcode={exit_code}")
        return lines
    log_state = latest_log_progress(log_path) if log_path else {}
    done: list[str] = list(dict.fromkeys(log_state.get("done_files") or []))
    encoded: list[str] = list(dict.fromkeys(log_state.get("encoded_files") or []))
    if inspect_outputs:
        tools = discover_tools()
        stream_dir = target / "BDMV" / "STREAM"
        for clip in clips:
            path = stream_dir / clip["file"]
            if path.exists():
                try:
                    info = inspect_clip(path, tools, accurate_video_bitrate=False)
                except ToolError:
                    info = {}
                if (info.get("video") or {}).get("codec_name") == "hevc" and (
                    desired_bit_depth is None or output_matches_hevc_bit_depth(info, desired_bit_depth)
                ):
                    done.append(clip["file"])
                    if clip["file"] not in encoded:
                        encoded.append(clip["file"])
        done = list(dict.fromkeys(done))
        encoded = list(dict.fromkeys(encoded))
    mux_file = log_state.get("mux_file")
    encode_file = log_state.get("encode_file")
    current_file = log_state.get("current_file")
    current_seconds = float(log_state.get("current_seconds") or 0)
    current_duration = 0.0
    current_percent = 0.0
    mux_percent = log_state.get("mux_percent")
    encode_seconds = float(log_state.get("encode_seconds") or 0)
    encode_percent = 0.0
    encode_duration = 0.0
    done_seconds = sum(float(clip.get("duration") or 0) for clip in clips if clip["file"] in done)
    encoded_seconds = sum(float(clip.get("duration") or 0) for clip in clips if clip["file"] in encoded)
    if current_file in done:
        current_seconds = 0
    elif current_file:
        current_duration = next((float(clip.get("duration") or 0) for clip in clips if clip["file"] == current_file), 0)
        if mux_percent is not None and not current_seconds and current_duration:
            current_seconds = current_duration * clamp_percent(float(mux_percent)) / 100.0
        current_seconds = min(current_seconds, current_duration)
        current_percent = clamp_percent(current_seconds / current_duration * 100.0) if current_duration else 0.0
    if encode_file and encode_file not in done:
        encode_duration = next((float(clip.get("duration") or 0) for clip in clips if clip["file"] == encode_file), 0)
        encode_seconds = min(encode_seconds, encode_duration)
        encode_percent = clamp_percent(encode_seconds / encode_duration * 100.0) if encode_duration else 0.0
    encoded_current_seconds = 0.0
    if encode_file and encode_file not in encoded:
        encoded_current_seconds = encode_seconds
    encoded_completed_seconds = encoded_seconds + encoded_current_seconds
    completed_seconds = done_seconds + current_seconds
    percent = clamp_percent((encoded_completed_seconds / total_seconds * 100.0) if total_seconds else (100.0 if exit_code == 0 else 0.0))
    bar = progress_bar(percent, args.width)
    if exit_code == 0:
        stage = "completed"
    elif exit_code is not None:
        stage = f"failed exitcode={exit_code}"
    elif log_state.get("current_stage"):
        stage = str(log_state["current_stage"])
    else:
        stage = "encoding" if current_file and current_seconds else ("post-processing or validation" if len(done) == len(clips) and clips else ("encoding/remuxing" if done else "scanning/copying/planning"))
    lines.append(f"{bar} {percent:5.1f}% encoded  {format_duration(encoded_completed_seconds)} / {format_duration(total_seconds)}  stage: {stage}")
    lines.append(f"encoded clips: {len(encoded)}/{len(clips)} complete")
    if done and len(done) != len(encoded):
        lines.append(f"muxed clips: {len(done)}/{len(clips)} complete")
    if mux_file and mux_file not in done:
        details = f"muxing:  {mux_file}  {progress_bar(current_percent, min(args.width, 24))} {current_percent:5.1f}%  {format_duration(current_seconds)} / {format_duration(current_duration)}"
        lines.append(details)
    if encode_file and encode_file not in done and encode_file != mux_file:
        details = f"encoding:{encode_file:>11}  {progress_bar(encode_percent, min(args.width, 24))} {encode_percent:5.1f}%  {format_duration(encode_seconds)} / {format_duration(encode_duration)}"
        if log_state.get("encode_speed"):
            details += f"  speed: {log_state['encode_speed']}"
        lines.append(details)
    if current_file and current_file not in done and not mux_file and not encode_file:
        details = f"current: {current_file}  {progress_bar(current_percent, min(args.width, 24))} {current_percent:5.1f}%  {format_duration(current_seconds)} / {format_duration(current_duration)}"
        if log_state.get("current_speed"):
            details += f"  speed: {log_state['current_speed']}"
        if mux_percent is not None:
            details += f"  mux: {clamp_percent(float(mux_percent)):5.1f}%"
        lines.append(details)
    if done:
        lines.append("done: " + ", ".join(done[-5:]))
    return lines


def print_progress(args: argparse.Namespace) -> None:
    print("\n".join(progress_lines(args)))


def cmd_progress(args: argparse.Namespace) -> int:
    renderer = WatchRenderer() if args.watch else None
    try:
        while True:
            lines = progress_lines(args)
            if renderer:
                renderer.render(lines)
            else:
                print("\n".join(lines))
            if not args.watch:
                return 0
            if args.log:
                log_path = Path(args.log).resolve()
                exit_path = log_path.with_name(log_path.stem + ".exitcode.txt")
                if exit_path.exists():
                    return safe_int(read_text_flexible(exit_path).strip()) or 0
            time.sleep(args.watch)
    finally:
        if renderer:
            renderer.close()


def enable_windows_virtual_terminal() -> bool:
    if os.name != "nt" or not sys.stdout.isatty():
        return os.name != "nt" and sys.stdout.isatty()
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def terminal_render_width(default: int = 100) -> int:
    return max(20, shutil.get_terminal_size((default, 20)).columns - 1)


def fit_terminal_line(line: str, width: int) -> str:
    line = line.replace("\t", "    ")
    if len(line) <= width:
        return line
    if width <= 3:
        return line[:width]
    return line[: width - 3] + "..."


class WatchRenderer:
    def __init__(self) -> None:
        self.ansi = enable_windows_virtual_terminal()
        self.previous_lines = 0
        self.hidden_cursor = False
        if self.ansi:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
            self.hidden_cursor = True

    def render(self, lines: list[str]) -> None:
        try:
            width = terminal_render_width()
            rendered = [fit_terminal_line(line, width) for line in lines]
            if not self.ansi:
                print("\n".join(rendered), flush=True)
                return
            if self.previous_lines:
                sys.stdout.write(f"\033[{self.previous_lines}F\033[J")
            sys.stdout.write("\n".join(rendered) + "\n")
            sys.stdout.flush()
            self.previous_lines = max(1, len(rendered))
        except (BrokenPipeError, OSError):
            raise SystemExit(0)

    def close(self) -> None:
        if self.hidden_cursor:
            try:
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                pass
            self.hidden_cursor = False
