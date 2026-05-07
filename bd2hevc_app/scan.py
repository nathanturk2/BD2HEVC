"""Disc scanning, FFprobe inspection, and MakeMKV title parsing."""

from __future__ import annotations

import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .bitrate import equivalent_hevc_bitrate, mbps, parse_rate, parse_timecode, safe_float, safe_int
from .config import (
    DEFAULT_MAKEMKV_TIMEOUT_SECONDS,
    MPEG2_SOURCE_CODECS,
    SECONDS_REENCODE_THRESHOLD,
    SPARSE_TIMING_ALWAYS_COUNT_MAX_DURATION,
    SPARSE_TIMING_FRAME_COUNT_MAX_DURATION,
    SPARSE_TIMING_MIN_GAP_SECONDS,
    SPARSE_TIMING_MIN_RATIO,
)
from .tools import ToolError, require_tool, run_cmd


def find_disc_roots(paths: list[Path]) -> list[Path]:
    roots: list[Path] = []
    for raw in paths:
        path = raw.resolve()
        if (path / "BDMV").is_dir():
            roots.append(path)
        elif path.name.upper() == "BDMV" and path.is_dir():
            roots.append(path.parent)
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir() and (child / "BDMV").is_dir():
                    roots.append(child.resolve())
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def split_robot_line(line: str) -> tuple[str, list[str]] | None:
    if ":" not in line:
        return None
    tag, rest = line.split(":", 1)
    try:
        fields = next(csv.reader([rest]))
    except csv.Error:
        return None
    return tag, fields


def run_makemkv_scan(
    source: Path,
    tools: dict[str, Any],
    *,
    timeout_seconds: float | None = DEFAULT_MAKEMKV_TIMEOUT_SECONDS,
    verbose: bool = False,
) -> dict[str, Any]:
    exe = tools.get("makemkvcon64") or tools.get("makemkvcon")
    if not exe:
        return {"available": False, "titles": [], "messages": ["MakeMKV CLI not found"]}
    cmd = [str(exe), "-r", "info", f"file:{source}"]
    result = run_cmd(cmd, check=False, capture=True, timeout_seconds=timeout_seconds, verbose=verbose)
    parsed = parse_makemkv_robot(result.stdout or "")
    parsed["available"] = True
    parsed["returncode"] = result.returncode
    parsed["timed_out"] = result.returncode == 124
    if parsed["timed_out"]:
        parsed.setdefault("messages", []).append(
            {"code": "TIMEOUT", "flags": "0", "severity": "2", "text": f"MakeMKV scan timed out after {timeout_seconds} seconds"}
        )
    parsed["stderr_tail"] = "\n".join((result.stderr or "").splitlines()[-20:])
    return parsed


def parse_makemkv_robot(text: str) -> dict[str, Any]:
    title_keys = {
        "2": "name",
        "8": "chapters",
        "9": "duration_text",
        "10": "size_text",
        "11": "size_bytes",
        "16": "source",
        "25": "segment_count",
        "26": "segments",
        "27": "mkv_name",
        "30": "display",
    }
    stream_keys = {
        "1": "type",
        "2": "channels_text",
        "3": "language_code",
        "4": "language",
        "5": "codec_id",
        "6": "codec_short",
        "7": "codec",
        "13": "bitrate_text",
        "14": "channels",
        "17": "sample_rate",
        "19": "resolution",
        "21": "fps_text",
        "30": "display",
        "38": "flags",
        "39": "flag_text",
    }
    titles: dict[int, dict[str, Any]] = {}
    messages: list[dict[str, str]] = []
    disc_info: dict[str, Any] = {}
    tcount: int | None = None

    for line in text.splitlines():
        parsed = split_robot_line(line)
        if not parsed:
            continue
        tag, fields = parsed
        if tag == "MSG" and len(fields) >= 4:
            messages.append(
                {
                    "code": fields[0],
                    "flags": fields[1],
                    "severity": fields[2],
                    "text": fields[3],
                }
            )
        elif tag == "TCOUNT" and fields:
            try:
                tcount = int(fields[0])
            except ValueError:
                pass
        elif tag == "CINFO" and len(fields) >= 3:
            disc_info[fields[0]] = fields[2]
        elif tag == "TINFO" and len(fields) >= 4:
            try:
                title_id = int(fields[0])
            except ValueError:
                continue
            title = titles.setdefault(title_id, {"id": title_id, "streams": {}})
            key = title_keys.get(fields[1])
            if key:
                title[key] = fields[3]
                if key == "duration_text":
                    title["duration"] = parse_timecode(fields[3])
                elif key == "size_bytes":
                    try:
                        title["size_bytes"] = int(fields[3])
                    except ValueError:
                        pass
                elif key == "segment_count":
                    try:
                        title["segment_count"] = int(fields[3])
                    except ValueError:
                        pass
                elif key == "segments":
                    title["segment_ids"] = [s.strip() for s in fields[3].split(",") if s.strip()]
        elif tag == "SINFO" and len(fields) >= 5:
            try:
                title_id = int(fields[0])
                stream_id = int(fields[1])
            except ValueError:
                continue
            title = titles.setdefault(title_id, {"id": title_id, "streams": {}})
            streams = title.setdefault("streams", {})
            stream = streams.setdefault(stream_id, {"id": stream_id})
            key = stream_keys.get(fields[2])
            if key:
                stream[key] = fields[4]

    compact_titles: list[dict[str, Any]] = []
    for title_id in sorted(titles):
        title = titles[title_id]
        streams_dict = title.pop("streams", {})
        title["streams"] = [streams_dict[idx] for idx in sorted(streams_dict)]
        compact_titles.append(title)

    return {
        "title_count": tcount if tcount is not None else len(compact_titles),
        "disc_info": disc_info,
        "titles": compact_titles,
        "messages": messages,
        "warnings": [m for m in messages if m.get("severity") and m["severity"] != "0"],
    }


def ffprobe_streams(path: Path, tools: dict[str, Any]) -> dict[str, Any]:
    ffprobe = require_tool(tools, "ffprobe")
    cmd = [
        ffprobe,
        "-hide_banner",
        "-v",
        "error",
        "-probesize",
        "500M",
        "-analyzeduration",
        "500M",
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,level,refs,has_b_frames,bit_rate,avg_frame_rate,r_frame_rate,start_time,duration:format=start_time,duration,bit_rate,size",
        "-of",
        "json",
        str(path),
    ]
    result = run_cmd(cmd, check=True, capture=True)
    return json.loads(result.stdout)


def sum_video_packet_bytes(path: Path, tools: dict[str, Any], *, video_selector: str = "v:0") -> int:
    ffprobe = require_tool(tools, "ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        video_selector,
        "-show_packets",
        "-show_entries",
        "packet=size",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = run_cmd(cmd)
    total = 0
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += int(line.split(",", 1)[0])
        except ValueError:
            continue
    return total


def count_video_frames(path: Path, tools: dict[str, Any], *, video_selector: str = "v:0") -> int | None:
    ffprobe = require_tool(tools, "ffprobe")
    cmd = [
        ffprobe,
        "-hide_banner",
        "-v",
        "error",
        "-select_streams",
        video_selector,
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "default=nw=1:nk=1",
        str(path),
    ]
    result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or line.upper() == "N/A":
            continue
        try:
            return int(line)
        except ValueError:
            return None
    return None


def inspect_clip(
    path: Path,
    tools: dict[str, Any],
    *,
    accurate_video_bitrate: bool = False,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "file": path.name,
        "path": str(path),
        "ok": False,
        "warnings": [],
    }
    try:
        probe = ffprobe_streams(path, tools)
    except Exception as exc:
        info["error"] = str(exc)
        return info

    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    duration = safe_float(fmt.get("duration"))
    total_bitrate = safe_int(fmt.get("bit_rate"))
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    known_audio_bps = sum(safe_int(s.get("bit_rate")) or 0 for s in audio)

    info.update(
        {
            "ok": True,
            "duration": duration,
            "format_start_time": safe_float(fmt.get("start_time")),
            "total_bitrate": total_bitrate,
        "video": compact_stream(video) if video else None,
            "audio": [compact_stream(s) for s in audio],
            "subtitles": [compact_stream(s) for s in subtitles],
        }
    )
    if video:
        fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")) or 23.976
        codec_name = str(video.get("codec_name") or "").lower()
        video_bps = safe_int(video.get("bit_rate"))
        bitrate_source = "stream"
        if accurate_video_bitrate and duration and duration > 0:
            packet_bytes = sum_video_packet_bytes(path, tools)
            if packet_bytes > 0:
                video_bps = int(packet_bytes * 8 / duration)
                bitrate_source = "video_packet_sum"
        elif total_bitrate and (
            not video_bps
            or codec_name in MPEG2_SOURCE_CODECS
            or (video_bps and video_bps > total_bitrate * 1.25)
        ):
            previous_video_bps = video_bps
            video_bps = max(0, total_bitrate - known_audio_bps)
            if previous_video_bps and previous_video_bps > total_bitrate * 1.25:
                bitrate_source = "container_minus_known_audio_stream_bitrate_implausible"
            elif codec_name in MPEG2_SOURCE_CODECS and previous_video_bps:
                bitrate_source = "container_minus_known_audio_mpeg2"
            else:
                bitrate_source = "container_minus_known_audio"
            if any(safe_int(s.get("bit_rate")) is None for s in audio):
                info["warnings"].append("video bitrate estimated; one or more audio streams did not expose bit_rate")
        target = equivalent_hevc_bitrate(
            video_bps=video_bps,
            width=safe_int(video.get("width")),
            height=safe_int(video.get("height")),
            fps=fps,
            duration_seconds=duration,
            source_codec=video.get("codec_name"),
            **(bitrate_options or {}),
        )
        info["video"].update(
            {
                "fps": fps,
                "source_video_bitrate": video_bps,
                "source_video_bitrate_mbps": mbps(video_bps),
                "source_video_bitrate_method": bitrate_source,
                "target_hevc": target,
            }
        )
        if duration and duration <= SPARSE_TIMING_FRAME_COUNT_MAX_DURATION:
            should_check_sparse = duration <= SPARSE_TIMING_ALWAYS_COUNT_MAX_DURATION
            sparse_source_fps_hint = parse_rate(video.get("r_frame_rate")) or fps
            should_check_sparse = should_check_sparse or sparse_source_fps_hint * SPARSE_TIMING_MIN_RATIO < fps
            frame_count = count_video_frames(path, tools) if should_check_sparse else None
            if frame_count and fps:
                nominal_frame_duration = frame_count / fps
                gap = duration - nominal_frame_duration
                sparse = gap > SPARSE_TIMING_MIN_GAP_SECONDS and duration / max(nominal_frame_duration, 0.001) >= SPARSE_TIMING_MIN_RATIO
                sparse_source_fps = sparse_source_fps_hint
                sparse_frame_interval = 1.0 / sparse_source_fps if sparse and sparse_source_fps and sparse_source_fps > 0 else None
                info["video"].update(
                    {
                        "decoded_frame_count": frame_count,
                        "nominal_frame_duration_seconds": round(nominal_frame_duration, 6),
                        "sparse_timestamp_video": sparse,
                        "sparse_timing_gap_seconds": round(gap, 6),
                        "sparse_source_fps": sparse_source_fps if sparse else None,
                        "sparse_final_hold_seconds": round(sparse_frame_interval, 6) if sparse_frame_interval else None,
                    }
                )
        if (duration or 0) > SECONDS_REENCODE_THRESHOLD and codec_name != "hevc":
            action = "reencode"
        elif (duration or 0) > SECONDS_REENCODE_THRESHOLD:
            action = "already_hevc"
        else:
            action = "copy"
        info["action"] = action
    else:
        info["action"] = "no_video"
    return info


def compact_stream(stream: dict[str, Any] | None) -> dict[str, Any]:
    if not stream:
        return {}
    keys = [
        "index",
        "codec_type",
        "codec_name",
        "profile",
        "width",
        "height",
        "pix_fmt",
        "level",
        "refs",
        "has_b_frames",
        "bit_rate",
        "avg_frame_rate",
        "r_frame_rate",
        "start_time",
        "duration",
    ]
    compact = {k: stream[k] for k in keys if k in stream}
    if "bit_rate" in compact:
        compact["bit_rate_mbps"] = mbps(safe_int(compact.get("bit_rate")))
    if "start_time" in compact:
        compact["start_time_seconds"] = safe_float(compact.get("start_time"))
    if "duration" in compact:
        compact["duration_seconds"] = safe_float(compact.get("duration"))
    return compact


def scan_disc(
    source: Path,
    tools: dict[str, Any],
    *,
    accurate_video_bitrate: bool,
    bitrate_options: dict[str, Any] | None = None,
    use_makemkv: bool = True,
    verbose: bool = False,
) -> dict[str, Any]:
    bdmv = source / "BDMV"
    stream_dir = bdmv / "STREAM"
    report: dict[str, Any] = {
        "disc": source.name,
        "source": str(source),
        "has_bdmv": bdmv.is_dir(),
        "clips": [],
        "makemkv": {},
    }
    if not stream_dir.is_dir():
        report["error"] = "BDMV/STREAM not found"
        return report
    report["makemkv"] = run_makemkv_scan(source, tools, verbose=verbose) if use_makemkv else {
        "available": bool(tools.get("makemkvcon64") or tools.get("makemkvcon")),
        "skipped": True,
        "titles": [],
        "messages": [{"code": "SKIPPED", "flags": "0", "severity": "1", "text": "MakeMKV scan skipped"}],
    }
    clips = sorted(stream_dir.glob("*.m2ts"))
    workers = min(4, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                inspect_clip,
                clip,
                tools,
                accurate_video_bitrate=accurate_video_bitrate and clip.stat().st_size > 100_000_000,
                bitrate_options=bitrate_options,
            ): clip
            for clip in clips
        }
        for future in as_completed(futures):
            report["clips"].append(future.result())
    report["clips"].sort(key=lambda c: c.get("file", ""))
    report["summary"] = summarize_disc(report)
    return report


def summarize_disc(report: dict[str, Any]) -> dict[str, Any]:
    clips = report.get("clips", [])
    titles = report.get("makemkv", {}).get("titles", [])
    reencode = [c for c in clips if c.get("action") == "reencode"]
    longest_clip = max((c for c in clips if c.get("duration")), key=lambda c: c.get("duration") or 0, default=None)
    longest_title = max((t for t in titles if t.get("duration")), key=lambda t: t.get("duration") or 0, default=None)
    return {
        "clip_count": len(clips),
        "reencode_clip_count": len(reencode),
        "longest_clip": clip_summary(longest_clip),
        "makemkv_title_count": len(titles),
        "main_title": title_summary(longest_title),
        "makemkv_returncode": report.get("makemkv", {}).get("returncode"),
    }


def clip_summary(clip: dict[str, Any] | None) -> dict[str, Any] | None:
    if not clip:
        return None
    video = clip.get("video") or {}
    target = video.get("target_hevc") or {}
    return {
        "file": clip.get("file"),
        "duration": clip.get("duration"),
        "codec": video.get("codec_name"),
        "source_video_bitrate_mbps": video.get("source_video_bitrate_mbps"),
        "target_hevc_mbps": target.get("target_mbps"),
        "target_hevc_cq": target.get("cq"),
        "rate_control": target.get("rate_control"),
    }


def title_summary(title: dict[str, Any] | None) -> dict[str, Any] | None:
    if not title:
        return None
    return {
        "id": title.get("id"),
        "source": title.get("source"),
        "duration": title.get("duration"),
        "duration_text": title.get("duration_text"),
        "segments": title.get("segment_ids"),
    }


def choose_title(makemkv: dict[str, Any], title_id: int | None) -> dict[str, Any]:
    titles = makemkv.get("titles") or []
    if title_id is not None:
        for title in titles:
            if title.get("id") == title_id:
                return title
        raise ToolError(f"MakeMKV title id not found: {title_id}")
    title = max((t for t in titles if t.get("duration")), key=lambda t: t.get("duration") or 0, default=None)
    if not title:
        raise ToolError("No MakeMKV title with duration was found")
    return title


def clip_path_for_title(source: Path, title: dict[str, Any]) -> Path | None:
    segment_ids = title.get("segment_ids") or []
    if len(segment_ids) != 1:
        return None
    return source / "BDMV" / "STREAM" / f"{int(segment_ids[0]):05d}.m2ts"
