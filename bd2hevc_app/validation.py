"""Output validation and Blu-ray playlist probing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bitrate import safe_float, safe_int
from .config import SECONDS_REENCODE_THRESHOLD
from .scan import inspect_clip, run_makemkv_scan, title_summary
from .tools import format_cmd, require_tool, run_cmd


def reference_start_time(clip_info: dict[str, Any]) -> float | None:
    video = clip_info.get("video") or {}
    for value in (video.get("start_time_seconds"), video.get("start_time"), clip_info.get("format_start_time")):
        seconds = safe_float(value)
        if seconds is not None:
            return seconds
    return None


def duration_match_tolerance(duration: float | None) -> float:
    if duration is None:
        return 0.5
    return max(0.5, min(2.0, duration * 0.005))


def validate_clip(
    source_clip: Path | None,
    output_clip: Path,
    tools: dict[str, Any],
    *,
    decode_seconds: float | None = None,
    require_hevc: str = "over-threshold",
) -> dict[str, Any]:
    result: dict[str, Any] = {"output": str(output_clip), "ok": False, "checks": []}
    out = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
    result["output_probe"] = out
    video = out.get("video") or {}
    duration = out.get("duration") or 0
    if require_hevc == "always" or (require_hevc == "over-threshold" and duration > SECONDS_REENCODE_THRESHOLD):
        result["checks"].append(
            {
                "name": "video_is_hevc",
                "ok": video.get("codec_name") == "hevc",
                "value": video.get("codec_name"),
                "duration": duration,
                "rule": require_hevc,
            }
        )
    elif require_hevc == "never":
        result["checks"].append({"name": "clip_probe_ok", "ok": bool(out.get("ok")), "duration": duration})
    else:
        result["checks"].append({"name": "short_clip_allowed", "ok": bool(out.get("ok")), "value": video.get("codec_name"), "duration": duration})
    if source_clip and source_clip.exists():
        src = inspect_clip(source_clip, tools, accurate_video_bitrate=False)
        src_audio = [a.get("codec_name") for a in src.get("audio", [])]
        out_audio = [a.get("codec_name") for a in out.get("audio", [])]
        result["checks"].append({"name": "audio_codecs_passthrough", "ok": src_audio == out_audio, "source": src_audio, "output": out_audio})
        src_start = reference_start_time(src)
        out_start = reference_start_time(out)
        if src_start is not None and out_start is not None:
            result["checks"].append(
                {
                    "name": "timestamp_start_matches_source",
                    "ok": abs(src_start - out_start) <= 0.05,
                    "source_start_time": src_start,
                    "output_start_time": out_start,
                    "tolerance_seconds": 0.05,
                }
            )
        src_duration = safe_float(src.get("duration"))
        out_duration = safe_float(out.get("duration"))
        if src_duration is not None and out_duration is not None and src_duration > SECONDS_REENCODE_THRESHOLD:
            tolerance = duration_match_tolerance(src_duration)
            result["checks"].append(
                {
                    "name": "duration_matches_source",
                    "ok": abs(src_duration - out_duration) <= tolerance,
                    "source_duration": src_duration,
                    "output_duration": out_duration,
                    "tolerance_seconds": tolerance,
                }
            )
        src_video = src.get("video") or {}
        if src_video.get("sparse_timestamp_video"):
            tolerance = duration_match_tolerance(src_duration)
            nominal_duration = safe_float(src_video.get("nominal_frame_duration_seconds"))
            collapsed = out_duration is not None and nominal_duration is not None and out_duration <= nominal_duration + tolerance
            result["checks"].append(
                {
                    "name": "sparse_timing_preserved",
                    "ok": src_duration is not None and out_duration is not None and abs(src_duration - out_duration) <= tolerance,
                    "source_duration": src_duration,
                    "output_duration": out_duration,
                    "source_frame_count_duration": nominal_duration,
                    "collapsed_to_frame_count_duration": collapsed,
                    "tolerance_seconds": tolerance,
                }
            )
            result["checks"].append(
                {
                    "name": "sparse_hevc_has_no_b_frames",
                    "ok": video.get("codec_name") != "hevc" or safe_int(video.get("has_b_frames")) == 0,
                    "codec": video.get("codec_name"),
                    "has_b_frames": safe_int(video.get("has_b_frames")),
                }
            )
    if decode_seconds and video:
        ffmpeg = require_tool(tools, "ffmpeg")
        cmd = [ffmpeg, "-hide_banner", "-v", "error", "-t", str(decode_seconds), "-i", str(output_clip), "-map", "0:v:0", "-f", "null", "-"]
        decode = run_cmd(cmd, check=False, capture=True)
        result["checks"].append({"name": "decode_sample", "ok": decode.returncode == 0, "stderr_tail": "\n".join((decode.stderr or "").splitlines()[-10:])})
    elif decode_seconds:
        result["checks"].append({"name": "decode_sample_skipped", "ok": True, "reason": "output clip has no video stream"})
    result["ok"] = all(c.get("ok") for c in result["checks"])
    return result


def validate_disc_titles(
    disc_root: Path,
    tools: dict[str, Any],
    *,
    expected_duration: float | None = None,
    min_titles: int = 1,
    tolerance_seconds: float = 120.0,
    use_makemkv: bool = True,
    require_makemkv: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    if not use_makemkv:
        return {
            "ok": True,
            "skipped": True,
            "checks": [{"name": "makemkv_validation_skipped", "ok": True}],
            "main_title": None,
            "messages": [{"code": "SKIPPED", "flags": "0", "severity": "1", "text": "MakeMKV validation skipped by option"}],
        }
    scan = run_makemkv_scan(disc_root, tools, verbose=verbose)
    if not scan.get("available"):
        return {
            "ok": not require_makemkv,
            "skipped": True,
            "checks": [{"name": "makemkv_available", "ok": not require_makemkv, "required": require_makemkv}],
            "main_title": None,
            "messages": scan.get("messages", []),
        }
    titles = scan.get("titles") or []
    longest = max((t for t in titles if t.get("duration")), key=lambda t: t.get("duration") or 0, default=None)
    checks = [
        {
            "name": "makemkv_scan_success",
            "ok": scan.get("available") and scan.get("returncode") == 0,
            "returncode": scan.get("returncode"),
        },
        {
            "name": "makemkv_titles_present",
            "ok": len(titles) >= min_titles,
            "title_count": len(titles),
            "min_titles": min_titles,
        },
    ]
    if expected_duration and expected_duration >= 120:
        longest_duration = longest.get("duration") if longest else None
        checks.append(
            {
                "name": "main_title_duration_matches",
                "ok": longest_duration is not None and abs(longest_duration - expected_duration) <= tolerance_seconds,
                "expected_duration": expected_duration,
                "longest_duration": longest_duration,
                "tolerance_seconds": tolerance_seconds,
            }
        )
    return {
        "ok": all(c.get("ok") for c in checks),
        "checks": checks,
        "main_title": title_summary(longest),
        "messages": scan.get("messages", [])[-20:],
    }


def ffprobe_bluray_playlist(
    disc_root: Path,
    playlist: int,
    tools: dict[str, Any],
    *,
    count_frames: bool = False,
) -> dict[str, Any]:
    ffprobe = require_tool(tools, "ffprobe")
    cmd = [ffprobe, "-hide_banner", "-v", "error"]
    if count_frames:
        cmd.append("-count_frames")
    cmd.extend(
        [
            "-playlist",
            str(playlist),
            "-show_entries",
            "format=duration,start_time:stream=index,codec_type,codec_name,start_time,duration,nb_read_frames,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            f"bluray:{disc_root}",
        ]
    )
    result = run_cmd(cmd, check=False, capture=True)
    try:
        probe = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        probe = {}
    return {
        "command": format_cmd(cmd),
        "returncode": result.returncode,
        "probe": probe,
        "stderr_tail": "\n".join((result.stderr or "").splitlines()[-40:]),
        "stderr": result.stderr or "",
    }


def video_frame_count_from_probe(probe: dict[str, Any]) -> int | None:
    counts: list[int] = []
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") == "video":
            value = safe_int(stream.get("nb_read_frames"))
            if value is not None:
                counts.append(value)
    return max(counts) if counts else None


def validate_bluray_playlist(
    disc_root: Path,
    playlist: int,
    tools: dict[str, Any],
    *,
    reference_root: Path | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    reference_tolerance: float = 2.0,
    count_frames: bool = False,
    min_video_frames: int | None = None,
    decode_seconds: float | None = None,
    fail_on_eof: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {"disc": str(disc_root), "playlist": playlist, "checks": []}
    probe = ffprobe_bluray_playlist(disc_root, playlist, tools, count_frames=count_frames or min_video_frames is not None)
    result["probe"] = {k: v for k, v in probe.items() if k != "stderr"}
    format_info = probe.get("probe", {}).get("format") or {}
    duration = safe_float(format_info.get("duration"))
    result["duration"] = duration
    result["checks"].append({"name": "ffprobe_playlist_opened", "ok": probe.get("returncode") == 0, "returncode": probe.get("returncode")})
    result["checks"].append({"name": "playlist_duration_present", "ok": duration is not None and duration > 0, "duration": duration})
    video_streams = [s for s in (probe.get("probe", {}).get("streams") or []) if s.get("codec_type") == "video"]
    result["checks"].append({"name": "video_stream_present", "ok": bool(video_streams), "video_streams": len(video_streams)})
    if fail_on_eof:
        stderr = probe.get("stderr") or ""
        result["checks"].append({"name": "no_read_past_eof", "ok": "Read past EOF" not in stderr, "matched": "Read past EOF" in stderr})
    if min_duration is not None:
        result["checks"].append({"name": "min_duration", "ok": duration is not None and duration >= min_duration, "duration": duration, "minimum": min_duration})
    if max_duration is not None:
        result["checks"].append({"name": "max_duration", "ok": duration is not None and duration <= max_duration, "duration": duration, "maximum": max_duration})
    if min_video_frames is not None:
        frames = video_frame_count_from_probe(probe.get("probe") or {})
        result["checks"].append({"name": "min_video_frames", "ok": frames is not None and frames >= min_video_frames, "frames": frames, "minimum": min_video_frames})
    if reference_root:
        reference_probe = ffprobe_bluray_playlist(reference_root, playlist, tools, count_frames=False)
        reference_duration = safe_float(((reference_probe.get("probe") or {}).get("format") or {}).get("duration"))
        result["reference_probe"] = {k: v for k, v in reference_probe.items() if k != "stderr"}
        result["checks"].append(
            {
                "name": "duration_matches_reference",
                "ok": duration is not None and reference_duration is not None and abs(duration - reference_duration) <= reference_tolerance,
                "duration": duration,
                "reference_duration": reference_duration,
                "tolerance_seconds": reference_tolerance,
            }
        )
    if decode_seconds:
        ffmpeg = require_tool(tools, "ffmpeg")
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-playlist",
            str(playlist),
            "-i",
            f"bluray:{disc_root}",
            "-t",
            str(decode_seconds),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ]
        decode = run_cmd(cmd, check=False, capture=True)
        decode_stderr = decode.stderr or ""
        result["decode"] = {"command": format_cmd(cmd), "returncode": decode.returncode, "stderr_tail": "\n".join(decode_stderr.splitlines()[-40:])}
        result["checks"].append(
            {
                "name": "decode_playlist_video_sample",
                "ok": decode.returncode == 0 and (not fail_on_eof or "Read past EOF" not in decode_stderr),
                "seconds": decode_seconds,
                "returncode": decode.returncode,
                "read_past_eof": "Read past EOF" in decode_stderr,
            }
        )
    result["ok"] = all(check.get("ok") for check in result["checks"])
    return result
