"""tsMuxeR track parsing and Blu-ray authoring helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bitrate import safe_float
from .tools import ToolError, require_tool, run_cmd


def quote_meta_path(path: Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def tsmuxer_start_time_clock(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    return str(int(round(seconds * 45_000)))


def reference_start_time(clip_info: dict[str, Any]) -> float | None:
    video = clip_info.get("video") or {}
    for value in (video.get("start_time_seconds"), video.get("start_time"), clip_info.get("format_start_time")):
        seconds = safe_float(value)
        if seconds is not None:
            return seconds
    return None


def parse_tsmuxer_tracks(input_path: Path, tools: dict[str, Any], *, verbose: bool = False) -> list[dict[str, str]]:
    tsmuxer = require_tool(tools, "tsmuxer")
    result = run_cmd([tsmuxer, str(input_path)], check=True, capture=True, verbose=verbose)
    tracks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in result.stdout.splitlines():
        if line.startswith("Track ID:"):
            if current:
                tracks.append(current)
            current = {"track": line.split(":", 1)[1].strip()}
        elif current and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            current[key] = value.strip()
    if current:
        tracks.append(current)
    return [t for t in tracks if t.get("stream_id")]


def write_tsmuxer_meta(input_path: Path, meta_path: Path, clip_info: dict[str, Any], tools: dict[str, Any]) -> list[dict[str, str]]:
    tracks = parse_tsmuxer_tracks(input_path, tools)
    video = clip_info.get("video") or {}
    fps = video.get("fps") or 23.976
    width = video.get("width") or 1920
    height = video.get("height") or 1080
    source = quote_meta_path(input_path)
    lines = ["MUXOPT --blu-ray-v3 --vbr --vbv-len=500"]
    for track in tracks:
        stream_id = track.get("stream_id", "")
        track_id = track.get("track")
        lang = track.get("stream_lang")
        options = [f"track={track_id}"]
        if lang:
            options.append(f"lang={lang}")
        if stream_id.startswith("V_"):
            if stream_id != "V_MPEGH/ISO/HEVC":
                continue
            options.append(f"fps={fps:.3f}")
        elif stream_id == "S_HDMV/PGS":
            options.extend([f"fps={fps:.3f}", f"video-width={width}", f"video-height={height}"])
        elif not stream_id.startswith("A_"):
            continue
        lines.append(f"{stream_id}, {source}, " + ", ".join(options))
    if len(lines) < 2:
        raise ToolError(f"tsMuxeR did not expose usable tracks for {input_path}")
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tracks


def write_tsmuxer_split_meta(
    video_input: Path,
    tracks_input: Path,
    meta_path: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    sample_seconds: float | None = None,
    sample_start: float = 0.0,
    include_audio: bool = True,
    include_subtitles: bool = True,
    video_track_id: str | None = None,
    start_time_seconds: float | None = None,
) -> list[dict[str, str]]:
    if sample_seconds and sample_start:
        raise ToolError("Split-source sample authoring only supports --sample-start 0, because tsMuxeR cuts all tracks globally.")
    tracks = parse_tsmuxer_tracks(tracks_input, tools)
    video = clip_info.get("video") or {}
    fps = video.get("fps") or 23.976
    width = video.get("width") or 1920
    height = video.get("height") or 1080
    muxopt = ["MUXOPT", "--blu-ray-v3", "--no-pcr-on-video-pid", "--vbr", "--vbv-len=500"]
    if sample_seconds:
        muxopt.insert(-1, f"--cut-end={sample_seconds}s")
    start_time_clock = tsmuxer_start_time_clock(start_time_seconds)
    if start_time_clock is not None:
        muxopt.append(f"--start-time={start_time_clock}")
    lines = [" ".join(muxopt)]
    video_options = [f"fps={fps:.3f}"]
    if video_track_id:
        video_options.insert(0, f"track={video_track_id}")
    lines.append(f"V_MPEGH/ISO/HEVC, {quote_meta_path(video_input)}, " + ", ".join(video_options))
    for track in tracks:
        stream_id = track.get("stream_id", "")
        if stream_id.startswith("V_"):
            continue
        track_id = track.get("track")
        if not track_id:
            continue
        lang = track.get("stream_lang")
        options = [f"track={track_id}"]
        if lang:
            options.append(f"lang={lang}")
        if stream_id == "S_HDMV/PGS":
            if not include_subtitles:
                continue
            options.extend([f"fps={fps:.3f}", f"video-width={width}", f"video-height={height}"])
        elif stream_id.startswith("A_"):
            if not include_audio:
                continue
        else:
            continue
        lines.append(f"{stream_id}, {quote_meta_path(tracks_input)}, " + ", ".join(options))
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tracks


def write_tsmuxer_m2ts_split_meta(
    video_input: Path,
    tracks_input: Path,
    meta_path: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    video_track_id: str | None = None,
    video_stream_id: str = "V_MPEGH/ISO/HEVC",
    audio_tracks_input: Path | None = None,
    compact_audio_tracks: list[dict[str, Any]] | None = None,
    start_time_seconds: float | None = None,
) -> list[dict[str, str]]:
    tracks = parse_tsmuxer_tracks(tracks_input, tools)
    audio_tracks = parse_tsmuxer_tracks(audio_tracks_input, tools) if audio_tracks_input else tracks
    video = clip_info.get("video") or {}
    fps = video.get("fps") or 23.976
    width = video.get("width") or 1920
    height = video.get("height") or 1080
    muxopt = ["MUXOPT", "--no-pcr-on-video-pid", "--vbr", "--new-audio-pes", "--vbv-len=500"]
    start_time_clock = tsmuxer_start_time_clock(start_time_seconds)
    if start_time_clock is not None:
        muxopt.append(f"--start-time={start_time_clock}")
    lines = [" ".join(muxopt)]
    video_options = [f"fps={fps:.3f}"]
    if video_track_id:
        video_options.insert(0, f"track={video_track_id}")
    lines.append(f"{video_stream_id}, {quote_meta_path(video_input)}, " + ", ".join(video_options))
    if compact_audio_tracks is not None:
        for audio in compact_audio_tracks:
            audio_path = Path(str(audio.get("path") or ""))
            if not audio_path:
                continue
            options = []
            lang = audio.get("language")
            if lang:
                options.append(f"lang={lang}")
            suffix = ", " + ", ".join(options) if options else ""
            lines.append(f"A_AC3, {quote_meta_path(audio_path)}{suffix}")
    else:
        for track in audio_tracks:
            stream_id = track.get("stream_id", "")
            if stream_id.startswith("V_") or not stream_id.startswith("A_"):
                continue
            track_id = track.get("track")
            if not track_id:
                continue
            lang = track.get("stream_lang")
            options = [f"track={track_id}"]
            if lang:
                options.append(f"lang={lang}")
            source = audio_tracks_input or tracks_input
            lines.append(f"{stream_id}, {quote_meta_path(source)}, " + ", ".join(options))
    for track in tracks:
        stream_id = track.get("stream_id", "")
        if stream_id.startswith("V_") or stream_id.startswith("A_"):
            continue
        track_id = track.get("track")
        if not track_id:
            continue
        lang = track.get("stream_lang")
        options = [f"track={track_id}"]
        if lang:
            options.append(f"lang={lang}")
        if stream_id == "S_HDMV/PGS":
            options.extend([f"fps={fps:.3f}", f"video-width={width}", f"video-height={height}"])
        else:
            continue
        lines.append(f"{stream_id}, {quote_meta_path(tracks_input)}, " + ", ".join(options))
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tracks


def author_m2ts_split(
    video_input: Path,
    tracks_input: Path,
    output_m2ts: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    video_track_id: str | None = None,
    video_stream_id: str = "V_MPEGH/ISO/HEVC",
    audio_tracks_input: Path | None = None,
    compact_audio_tracks: list[dict[str, Any]] | None = None,
    reference_clip_info: dict[str, Any] | None = None,
    verbose: bool = False,
) -> Path:
    tsmuxer = require_tool(tools, "tsmuxer")
    meta_path = output_m2ts.with_suffix(".meta")
    write_tsmuxer_m2ts_split_meta(
        video_input,
        tracks_input,
        meta_path,
        clip_info,
        tools,
        video_track_id=video_track_id,
        video_stream_id=video_stream_id,
        audio_tracks_input=audio_tracks_input,
        compact_audio_tracks=compact_audio_tracks,
        start_time_seconds=reference_start_time(reference_clip_info or clip_info),
    )
    run_cmd([tsmuxer, str(meta_path), str(output_m2ts)], check=True, capture=False, verbose=verbose)
    return meta_path


def first_video_track_id(input_path: Path, tools: dict[str, Any]) -> str | None:
    for track in parse_tsmuxer_tracks(input_path, tools):
        if (track.get("stream_id") or "").startswith("V_"):
            return track.get("track")
    return None


def author_uhdbd(input_m2ts: Path, output: Path, clip_info: dict[str, Any], tools: dict[str, Any], *, dry_run: bool, verbose: bool) -> Path:
    tsmuxer = require_tool(tools, "tsmuxer")
    meta_path = input_m2ts.with_suffix(".uhdbd.meta")
    if dry_run:
        tracks = parse_tsmuxer_tracks(input_m2ts, tools, verbose=verbose) if input_m2ts.exists() else []
        print(json.dumps({"would_author": str(output), "input": str(input_m2ts), "tracks": tracks}, indent=2))
        return meta_path
    write_tsmuxer_meta(input_m2ts, meta_path, clip_info, tools)
    output.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([tsmuxer, str(meta_path), str(output)], check=True, capture=False, verbose=verbose)
    return meta_path


def author_uhdbd_split(
    video_input: Path,
    tracks_input: Path,
    output: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    sample_seconds: float | None,
    sample_start: float,
    include_audio: bool,
    include_subtitles: bool,
    dry_run: bool,
    verbose: bool,
) -> Path:
    tsmuxer = require_tool(tools, "tsmuxer")
    meta_path = video_input.with_suffix(".uhdbd.meta")
    if dry_run:
        tracks = parse_tsmuxer_tracks(tracks_input, tools, verbose=verbose) if tracks_input.exists() else []
        print(json.dumps({"would_author": str(output), "video": str(video_input), "tracks_from": str(tracks_input), "tracks": tracks}, indent=2))
        return meta_path
    write_tsmuxer_split_meta(
        video_input,
        tracks_input,
        meta_path,
        clip_info,
        tools,
        sample_seconds=sample_seconds,
        sample_start=sample_start,
        include_audio=include_audio,
        include_subtitles=include_subtitles,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([tsmuxer, str(meta_path), str(output)], check=True, capture=False, verbose=verbose)
    return meta_path
