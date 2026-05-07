"""Output repair helpers for converted Blu-ray folders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bitrate import safe_float
from .config import SECONDS_REENCODE_THRESHOLD
from .encoding import encode_to_hevc_m2ts
from .muxing import author_m2ts_split, first_video_track_id, reference_start_time
from .navigation import restore_source_clpi
from .output import replace_file_with_retry
from .progress import normalize_clip_name, output_matches_hevc_bit_depth
from .scan import inspect_clip
from .tools import ToolError, format_cmd
from .validation import validate_clip


def duration_match_tolerance(duration: float | None) -> float:
    if duration is None:
        return 0.5
    return max(0.5, min(2.0, duration * 0.005))


def remux_replacement_clip(
    source_clip: Path,
    output_clip: Path,
    output_clpi: Path,
    tools: dict[str, Any],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    if not source_clip.exists():
        raise ToolError(f"Source clip is missing for remux repair: {source_clip}")
    if not output_clip.exists():
        raise ToolError(f"Output clip is missing for remux repair: {output_clip}")
    clip_info = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
    source_clip_info = inspect_clip(source_clip, tools, accurate_video_bitrate=False)
    video = clip_info.get("video") or {}
    if video.get("codec_name") != "hevc":
        raise ToolError(f"Output clip is not HEVC and cannot be remux repaired: {output_clip}")
    video_track_id = first_video_track_id(output_clip, tools)
    if not video_track_id:
        raise ToolError(f"Could not find HEVC video track in {output_clip}")
    temp_output = output_clip.with_name(f"{output_clip.stem}.remux.tmp.m2ts")
    temp_output.unlink(missing_ok=True)
    replace_report = None
    try:
        meta_path = author_m2ts_split(
            output_clip,
            source_clip,
            temp_output,
            clip_info,
            tools,
            video_track_id=video_track_id,
            reference_clip_info=source_clip_info,
            verbose=verbose,
        )
        replace_report = replace_file_with_retry(temp_output, output_clip, verbose=verbose)
    finally:
        if replace_report and temp_output.exists():
            temp_output.unlink(missing_ok=True)
    clpi_report = restore_source_clpi(source_clip, output_clpi, output_clip=output_clip)
    repaired = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
    return {
        "clip": output_clip.name,
        "source": str(source_clip),
        "output": str(output_clip),
        "clpi": str(output_clpi),
        "video_track": video_track_id,
        "authoring": {
            "m2ts": str(output_clip),
            "meta": str(meta_path),
            "mode": "timestamp-matched-m2ts-preserve-source-clpi",
            "start_time_seconds": reference_start_time(source_clip_info),
        },
        "replace": replace_report,
        "clpi_restore": clpi_report,
        "source_start_time_seconds": reference_start_time(source_clip_info),
        "output_start_time_seconds": reference_start_time(repaired),
        "ok": bool(repaired.get("ok")) and (repaired.get("video") or {}).get("codec_name") == "hevc",
    }


def reencode_replacement_clip(
    source_clip: Path,
    output_clip: Path,
    output_clpi: Path,
    tools: dict[str, Any],
    *,
    hevc_bit_depth: int,
    encoder: str = "hevc_nvenc",
    decode_sample: float | None = None,
    bitrate_options: dict[str, Any] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    if not source_clip.exists():
        raise ToolError(f"Source clip is missing for reencode repair: {source_clip}")
    source_clip_info = inspect_clip(source_clip, tools, accurate_video_bitrate=False, bitrate_options=bitrate_options)
    video = source_clip_info.get("video") or {}
    if not source_clip_info.get("ok") or not video:
        raise ToolError(f"Could not inspect source clip for reencode repair: {source_clip}")
    temp_video = output_clip.with_name(f"{output_clip.stem}.reencode.tmp.hevc")
    temp_output = output_clip.with_name(f"{output_clip.stem}.reencode.tmp.m2ts")
    temp_video.unlink(missing_ok=True)
    temp_output.unlink(missing_ok=True)
    replace_report = None
    try:
        encode_cmd = encode_to_hevc_m2ts(
            source_clip,
            temp_video,
            source_clip_info,
            tools,
            video_only=True,
            hevc_bit_depth=hevc_bit_depth,
            encoder=encoder,
            dry_run=False,
            verbose=verbose,
        )
        meta_path = author_m2ts_split(
            temp_video,
            source_clip,
            temp_output,
            source_clip_info,
            tools,
            reference_clip_info=source_clip_info,
            verbose=verbose,
        )
        replace_report = replace_file_with_retry(temp_output, output_clip, verbose=verbose)
    finally:
        temp_video.unlink(missing_ok=True)
        if replace_report and temp_output.exists():
            temp_output.unlink(missing_ok=True)
    clpi_report = restore_source_clpi(source_clip, output_clpi, output_clip=output_clip)
    validation = validate_clip(source_clip, output_clip, tools, decode_seconds=decode_sample, require_hevc="always")
    repaired = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
    return {
        "clip": output_clip.name,
        "source": str(source_clip),
        "output": str(output_clip),
        "clpi": str(output_clpi),
        "hevc_bit_depth": hevc_bit_depth,
        "encoder": encoder,
        "encode_command": format_cmd(encode_cmd),
        "authoring": {
            "m2ts": str(output_clip),
            "meta": str(meta_path),
            "mode": "reencoded-timestamp-matched-m2ts-preserve-source-clpi",
            "start_time_seconds": reference_start_time(source_clip_info),
        },
        "replace": replace_report,
        "clpi_restore": clpi_report,
        "output_profile": (repaired.get("video") or {}).get("profile"),
        "output_pix_fmt": (repaired.get("video") or {}).get("pix_fmt"),
        "validation": validation,
        "ok": bool(validation.get("ok")),
    }

def select_output_repair_clips(
    source_root: Path,
    output_root: Path,
    tools: dict[str, Any],
    *,
    hevc_bit_depth: int,
    requested_clips: list[str] | None = None,
) -> list[dict[str, Any]]:
    source_stream = source_root / "BDMV" / "STREAM"
    output_stream = output_root / "BDMV" / "STREAM"
    names = [normalize_clip_name(clip) for clip in requested_clips] if requested_clips else [
        path.name for path in sorted(source_stream.glob("*.m2ts"))
    ]
    selected: list[dict[str, Any]] = []
    for name in names:
        source_clip = source_stream / name
        output_clip = output_stream / name
        if not source_clip.exists():
            if requested_clips:
                raise ToolError(f"Requested source clip does not exist: {source_clip}")
            continue
        if not output_clip.exists():
            if requested_clips:
                raise ToolError(f"Requested output clip does not exist: {output_clip}")
            continue
        source_info = inspect_clip(source_clip, tools, accurate_video_bitrate=False)
        source_video = source_info.get("video") or {}
        duration = float(source_info.get("duration") or 0)
        source_codec = str(source_video.get("codec_name") or "").lower()
        if not requested_clips:
            if duration <= SECONDS_REENCODE_THRESHOLD:
                continue
            if source_codec == "hevc":
                continue
        output_info = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
        output_video = output_info.get("video") or {}
        output_duration = safe_float(output_info.get("duration"))
        duration_mismatch = duration > SECONDS_REENCODE_THRESHOLD and (
            output_duration is None or abs(duration - output_duration) > duration_match_tolerance(duration)
        )
        if output_video.get("codec_name") != "hevc":
            reason = "output_not_hevc"
        elif not output_matches_hevc_bit_depth(output_info, hevc_bit_depth):
            reason = "hevc_bit_depth_mismatch"
        elif duration_mismatch:
            reason = "duration_mismatch"
        elif requested_clips:
            reason = "requested"
        else:
            continue
        selected.append(
            {
                "clip": name,
                "reason": reason,
                "duration": duration,
                "source_video": source_video,
                "output_video": output_video,
            }
        )
    return selected

