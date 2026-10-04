#!/usr/bin/env python3
"""
BD2HEVC full-disc Blu-ray HEVC conversion.

This tool is built for local, unencrypted BDMV backups. It uses FFprobe/FFmpeg
for stream inspection and HEVC encoding, tsMuxeR for Blu-ray M2TS authoring, and
optionally MakeMKV/VLC for validation.
"""

from __future__ import annotations

import argparse
import copy
import json
import queue as thread_queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .bdj import (
    compatibility_fix_names_from_args,
    custom_compatibility_patch_files_from_args,
    patch_bluray_vlc_menu,
    patch_known_bdj_compatibility,
)
from .locking import FileLock, inherited_work_slot
from .runtime_support import swap_directory
from .bitrate import (
    bitrate_options_from_args,
    equivalent_hevc_bitrate,
    format_duration,
    mbps,
    normalize_bitrate_mode,
    parse_bitrate_arg,
    parse_duration_arg,
    parse_rate,
    parse_timecode,
    safe_float,
    safe_int,
)
from .config import (
    ANIME_CQ_PRESET,
    AUDIO_MODES,
    ANIME_CQ_VALUE,
    BITRATE_MODES,
    DEINTERLACE_FILTERS,
    DEINTERLACE_MODES,
    DEFAULT_ANIME_CQ_MIN_DURATION,
    DEFAULT_AUDIO_MODE,
    DEFAULT_DEINTERLACE_MODE,
    DEFAULT_MAKEMKV_TIMEOUT_SECONDS,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_REPORT_DIR,
    DEFAULT_JOB_DIR,
    DEFAULT_STEREO_AUDIO_BITRATE,
    DEFAULT_VLC_COMPATIBILITY_MODE,
    HEVC_ENCODERS,
    INTERLACED_FIELD_ORDERS,
    KNOWN_VLC_COMPATIBILITY_FIXES,
    LEGACY_ANIME_CQ_PRESET,
    LEGACY_EPISODE_COMPACT_PRESET,
    MPEG2_SOURCE_CODECS,
    ROOT,
    SECONDS_REENCODE_THRESHOLD,
    SPARSE_TIMING_ALWAYS_COUNT_MAX_DURATION,
    SPARSE_TIMING_FRAME_COUNT_MAX_DURATION,
    SPARSE_TIMING_MIN_GAP_SECONDS,
    SPARSE_TIMING_MIN_RATIO,
    VERSION,
)
from .diagnostics import DEFAULT_DIAGNOSTIC_LOG_LINES, cmd_diagnose
from .encoding import compact_audio_source_streams, encode_to_hevc_m2ts, transcode_compact_audio_tracks
from .libbluray_record import create_libbluray_recording, isolated_bdj_storage_env, libbluray_debug_env
from .iso import author_bluray_iso, find_udf_tool, iso_output_path, iso_staging_path, verify_bluray_iso
from .muxing import (
    author_m2ts_split,
    author_uhdbd_split,
    parse_tsmuxer_tracks,
    write_tsmuxer_meta,
)
from .navigation import (
    main_feature_selection,
    patch_clpi_for_output,
    patch_navigation_for_hevc,
    restore_source_clpi,
    scale_clpi_cpi_map_to_stream,
)
from .output import (
    conversion_succeeded,
    copy_disc_tree_skipping_reencoded_streams,
    default_output_for,
    ensure_disc_library_metadata,
    generated_output_for,
    make_output_available,
    path_or_none,
    print_conversion_summary,
    safe_name,
    validate_output_available,
)
from .progress import (
    cmd_progress,
    emit_conversion_progress,
    fit_terminal_line,
    progress_event,
    read_text_flexible,
)
from .presets import (
    apply_named_preset_to_args,
    cmd_preset_list,
    cmd_preset_remove,
    cmd_preset_save,
    cmd_preset_show,
)
from .queueing import (
    auto_command_for_job,
    cmd_cancel_job,
    cmd_jobs,
    cmd_pause_queue,
    cmd_remove_job,
    cmd_resume_queue,
    cmd_run_job,
    cmd_status,
    job_paths,
    known_job_files,
    try_load_job,
    save_job,
    start_background_process,
)
from .repair import (
    reencode_replacement_clip,
    remux_replacement_clip,
    select_output_repair_clips,
)
from .scan import (
    choose_title,
    clip_path_for_title,
    clip_summary,
    ffprobe_streams,
    find_disc_roots,
    inspect_clip,
    refine_disc_video_bitrates,
    run_makemkv_scan,
    scan_disc,
    summarize_disc,
    title_summary,
)
from .tools import (
    ToolError,
    discover_tools,
    encoder_is_hardware,
    format_cmd,
    refreshed_env,
    require_hevc_encoder,
    require_working_hevc_encoder,
    require_tool,
    run_cmd,
    selected_hevc_encoder,
)
from .uhd import (
    DISC_SIZE_BYTES,
    ensure_uhd_backup_structure,
    fit_reencoded_clips_to_disc_size,
)
from .validation import (
    ffprobe_bluray_playlist,
    validate_bluray_playlist,
    validate_clip,
    validate_disc_titles,
)


def use_makemkv_from_args(args: argparse.Namespace) -> bool:
    if getattr(args, "no_makemkv", False):
        return False
    return bool(getattr(args, "makemkv", False) or getattr(args, "require_makemkv", False))


def audio_mode_from_args(args: argparse.Namespace) -> str:
    return str(getattr(args, "audio_mode", DEFAULT_AUDIO_MODE) or DEFAULT_AUDIO_MODE)


def stereo_audio_bitrate_from_args(args: argparse.Namespace) -> int:
    return int(getattr(args, "stereo_audio_bitrate", DEFAULT_STEREO_AUDIO_BITRATE) or DEFAULT_STEREO_AUDIO_BITRATE)


def mono_audio_bitrate_from_args(args: argparse.Namespace) -> int:
    return int(getattr(args, "mono_audio_bitrate", DEFAULT_MONO_AUDIO_BITRATE) or DEFAULT_MONO_AUDIO_BITRATE)


def flatten_clip_values(values: Any) -> list[str]:
    flattened: list[str] = []
    for value in values or []:
        if isinstance(value, (list, tuple)):
            flattened.extend(str(item) for item in value)
        else:
            flattened.append(str(value))
    return flattened


def flatten_clip_pairs(values: Any) -> list[tuple[str, Any]]:
    flattened: list[tuple[str, Any]] = []
    for value in values or []:
        if isinstance(value, (list, tuple)) and len(value) == 2 and not isinstance(value[0], (list, tuple)):
            flattened.append((str(value[0]), value[1]))
        else:
            raise ToolError("Clip quality overrides must be CLIP VALUE pairs")
    return flattened


def normalize_clip_name(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ToolError("Clip id cannot be empty")
    name = Path(text.replace("\\", "/")).name
    path = Path(name)
    suffix = path.suffix.lower()
    if suffix and suffix != ".m2ts":
        raise ToolError(f"Clip id must be an M2TS clip id or filename, not {value!r}")
    return name if suffix else f"{name}.m2ts"


def normalize_clip_names(values: Any) -> list[str]:
    return [normalize_clip_name(value) for value in flatten_clip_values(values)]


def clip_lookup_by_name(clips: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for clip in clips:
        if clip.get("file"):
            lookup[normalize_clip_name(clip["file"])] = clip
    return lookup


def require_named_clips(clips: list[dict[str, Any]], names: list[str], *, option: str) -> list[dict[str, Any]]:
    lookup = clip_lookup_by_name(clips)
    missing = sorted({name for name in names if name not in lookup})
    if missing:
        raise ToolError(f"{option} referenced unknown clip(s): {', '.join(missing)}")
    return [lookup[name] for name in names]


COPY_QUALITY_ALIASES = {"copy", "source", "passthrough", "no-reencode", "no-reencoding", "none"}


def original_clip_action(clip: dict[str, Any]) -> str | None:
    return clip.get("original_action") or clip.get("action")


def remember_original_clip_actions(clips: list[dict[str, Any]]) -> None:
    for clip in clips:
        clip.setdefault("original_action", clip.get("action"))


def clip_is_reencode_eligible(clip: dict[str, Any]) -> bool:
    return original_clip_action(clip) == "reencode"


def reencode_quality_candidates(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [clip for clip in clips if clip_is_reencode_eligible(clip)]
    return sorted(candidates, key=lambda item: float(item.get("duration") or 0), reverse=True)


def validate_bitrate_mode_value(value: Any, *, option: str) -> str:
    mode = normalize_bitrate_mode(str(value or "").strip())
    if mode not in BITRATE_MODES:
        allowed = ", ".join(BITRATE_MODES)
        raise ToolError(f"{option} must be one of: {allowed}")
    return mode


def validate_cq_value(value: Any, *, option: str) -> int:
    try:
        cq_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{option} must be an integer between 0 and 51") from exc
    if cq_value < 0 or cq_value > 51:
        raise ToolError(f"{option} must be between 0 and 51")
    return cq_value


def validate_factor_value(value: Any, *, option: str) -> float:
    try:
        factor = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{option} must be a number greater than zero") from exc
    if factor <= 0:
        raise ToolError(f"{option} must be greater than zero")
    return factor


def parse_quality_spec(value: Any, *, option: str) -> dict[str, Any] | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        raise ToolError(f"{option} cannot be empty")
    if text in COPY_QUALITY_ALIASES:
        return {"action": "copy", "quality": "copy"}
    cq_match = re.fullmatch(r"(?:compact-cq|cq)[:= -]?(\d{1,2})", text) or re.fullmatch(r"cq(\d{1,2})", text)
    if cq_match:
        cq_value = validate_cq_value(cq_match.group(1), option=option)
        return {"action": "reencode", "quality": f"cq:{cq_value}", "mode": ANIME_CQ_PRESET, "cq": cq_value}
    if text == LEGACY_ANIME_CQ_PRESET:
        return {
            "action": "reencode",
            "quality": LEGACY_ANIME_CQ_PRESET,
            "mode": ANIME_CQ_PRESET,
            "cq": ANIME_CQ_VALUE,
            "legacy_preset": LEGACY_ANIME_CQ_PRESET,
        }
    if text == LEGACY_EPISODE_COMPACT_PRESET:
        return {
            "action": "reencode",
            "quality": LEGACY_EPISODE_COMPACT_PRESET,
            "mode": ANIME_CQ_PRESET,
            "legacy_preset": LEGACY_EPISODE_COMPACT_PRESET,
        }
    ratio_match = (
        re.fullmatch(r"(?:source-ratio|source_ratio|ratio|factor)[:=](\d+(?:\.\d+)?)", text)
        or re.fullmatch(r"(\d+(?:\.\d+)?)x", text)
    )
    if ratio_match:
        factor = validate_factor_value(ratio_match.group(1), option=option)
        return {
            "action": "reencode",
            "quality": f"source-ratio:{factor:g}",
            "mode": "source-ratio",
            "factor_override": factor,
        }
    mode = validate_bitrate_mode_value(text, option=option)
    return {"action": "reencode", "quality": mode, "mode": mode}


def quality_spec_bitrate_options(base_options: dict[str, Any] | None, spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("action") == "copy":
        return copy.deepcopy(base_options or {})
    options = override_bitrate_options(base_options, mode=spec.get("mode"), cq_value=spec.get("cq"))
    if spec.get("factor_override") is not None:
        options["factor_override"] = validate_factor_value(spec.get("factor_override"), option="quality factor")
    return options


def quality_spec_uses_cq(spec: dict[str, Any] | None) -> bool:
    if not spec or spec.get("action") == "copy":
        return False
    return spec.get("cq") is not None or normalize_bitrate_mode(str(spec.get("mode") or "")) == ANIME_CQ_PRESET


def args_request_cq_quality(args: argparse.Namespace, *, general_options: dict[str, Any]) -> bool:
    if normalize_bitrate_mode(str(general_options.get("mode") or "balanced")) == ANIME_CQ_PRESET:
        return True
    if quality_spec_uses_cq(parse_quality_spec(getattr(args, "quality", None), option="--quality")):
        return True
    if quality_spec_uses_cq(parse_quality_spec(getattr(args, "main_title_quality", None), option="--main-title-quality")):
        return True
    if getattr(args, "main_title_cq", None) is not None:
        return True
    if getattr(args, "main_title_bitrate_mode", None) and normalize_bitrate_mode(str(args.main_title_bitrate_mode)) == ANIME_CQ_PRESET:
        return True
    top_n_quality = parse_top_n_quality(getattr(args, "top_n_quality", None), option="--top-n-quality")
    if top_n_quality and quality_spec_uses_cq(top_n_quality[1]):
        return True
    if getattr(args, "top_n_cq", None):
        return True
    top_n_mode = parse_top_n_mode(getattr(args, "top_n_bitrate_mode", None), option="--top-n-bitrate-mode")
    if top_n_mode and normalize_bitrate_mode(top_n_mode[1]) == ANIME_CQ_PRESET:
        return True
    for _, quality in flatten_clip_pairs(getattr(args, "clip_quality", None)):
        if quality_spec_uses_cq(parse_quality_spec(quality, option="--clip-quality QUALITY")):
            return True
    if getattr(args, "clip_cq", None):
        return True
    for _, mode in flatten_clip_pairs(getattr(args, "clip_bitrate_mode", None)):
        if normalize_bitrate_mode(str(mode)) == ANIME_CQ_PRESET:
            return True
    return False


def normalize_uhd_profile(value: Any) -> str:
    text = str(value or "library").strip().lower()
    if text in {"", "auto", "library", "digital"}:
        return "library"
    if text in {"disc", "physical", "bd25", "bd-r", "bdr"}:
        return "disc"
    if text == "off":
        return "library"
    raise ToolError("--uhd-profile must be library or disc")


def depad_video_padding_from_args(args: argparse.Namespace) -> bool:
    if getattr(args, "keep_source_padding", False):
        return False
    return normalize_uhd_profile(getattr(args, "uhd_profile", "library")) != "disc"


def patch_version_headers_from_args(args: argparse.Namespace) -> bool:
    return normalize_uhd_profile(getattr(args, "uhd_profile", "library")) == "disc"


def bitrate_options_for_args(args: argparse.Namespace) -> dict[str, Any]:
    options = bitrate_options_from_args(args)
    spec = parse_quality_spec(getattr(args, "quality", None), option="--quality")
    if spec and spec.get("action") == "reencode":
        return quality_spec_bitrate_options(options, spec)
    return options


def parse_top_n_mode(value: Any, *, option: str) -> tuple[int, str] | None:
    if not value:
        return None
    if len(value) != 2:
        raise ToolError(f"{option} requires COUNT and MODE")
    try:
        count = int(value[0])
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{option} COUNT must be an integer") from exc
    if count < 1:
        raise ToolError(f"{option} COUNT must be at least 1")
    mode = validate_bitrate_mode_value(value[1], option=f"{option} MODE")
    return count, mode


def parse_top_n_quality(value: Any, *, option: str) -> tuple[int, dict[str, Any]] | None:
    if not value:
        return None
    if len(value) != 2:
        raise ToolError(f"{option} requires COUNT and QUALITY")
    try:
        count = int(value[0])
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{option} COUNT must be an integer") from exc
    if count < 1:
        raise ToolError(f"{option} COUNT must be at least 1")
    spec = parse_quality_spec(value[1], option=f"{option} QUALITY")
    if spec is None:
        raise ToolError(f"{option} QUALITY cannot be empty")
    return count, spec


def parse_top_n_cq(value: Any, *, option: str = "--top-n-cq") -> tuple[int, int] | None:
    if not value:
        return None
    if len(value) != 2:
        raise ToolError(f"{option} requires COUNT and CQ")
    try:
        count = int(value[0])
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{option} COUNT must be an integer") from exc
    if count < 1:
        raise ToolError(f"{option} COUNT must be at least 1")
    return count, validate_cq_value(value[1], option=f"{option} CQ")


def override_bitrate_options(
    base_options: dict[str, Any] | None,
    *,
    mode: str | None = None,
    cq_value: int | None = None,
) -> dict[str, Any]:
    options = copy.deepcopy(base_options or {})
    if mode is not None:
        options["mode"] = validate_bitrate_mode_value(mode, option="bitrate override mode")
    if cq_value is not None:
        options["mode"] = ANIME_CQ_PRESET
        options["compact_cq_value"] = validate_cq_value(cq_value, option="CQ override")
    if cq_value is not None:
        current_min = safe_float(options.get("anime_cq_min_duration")) or DEFAULT_ANIME_CQ_MIN_DURATION
        options["anime_cq_min_duration"] = min(current_min, SECONDS_REENCODE_THRESHOLD)
    return options


def summarize_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": target.get("mode"),
        "rate_control": target.get("rate_control"),
        "cq": target.get("cq"),
        "target_mbps": target.get("target_mbps"),
        "maxrate_mbps": target.get("maxrate_mbps"),
        "bufsize_mbps": target.get("bufsize_mbps"),
    }


def retarget_clip(
    clip: dict[str, Any],
    bitrate_options: dict[str, Any],
    *,
    override_kind: str,
    override_label: str,
) -> dict[str, Any]:
    video = clip.setdefault("video", {})
    previous = copy.deepcopy(video.get("target_hevc") or {})
    target = equivalent_hevc_bitrate(
        video_bps=safe_int(video.get("source_video_bitrate")) or safe_int(video.get("bit_rate")),
        width=safe_int(video.get("width")),
        height=safe_int(video.get("height")),
        fps=safe_float(video.get("fps")) or parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")),
        duration_seconds=safe_float(clip.get("duration")),
        source_codec=video.get("codec_name"),
        **bitrate_options,
    )
    target[f"{override_kind}_override"] = True
    reason = target.get("reason")
    target["reason"] = f"{reason}; {override_label}" if reason else override_label
    video["target_hevc"] = target
    clip["action"] = "reencode"
    return {
        "file": clip.get("file"),
        "duration": clip.get("duration"),
        "action": clip.get("action"),
        "previous": summarize_target(previous),
        "target": summarize_target(target),
    }


def copy_clip_for_quality_override(clip: dict[str, Any], *, override_kind: str, override_label: str) -> dict[str, Any]:
    previous_action = clip.get("action")
    clip["action"] = "copy"
    clip[f"{override_kind}_override"] = True
    clip["copy_override_reason"] = override_label
    return {
        "file": clip.get("file"),
        "duration": clip.get("duration"),
        "previous_action": previous_action,
        "action": clip.get("action"),
        "quality": "copy",
    }


def apply_quality_spec_to_clip(
    clip: dict[str, Any],
    spec: dict[str, Any],
    bitrate_options: dict[str, Any],
    *,
    override_kind: str,
    override_label: str,
) -> dict[str, Any]:
    if spec.get("action") == "copy":
        return copy_clip_for_quality_override(clip, override_kind=override_kind, override_label=override_label)
    options = quality_spec_bitrate_options(bitrate_options, spec)
    report = retarget_clip(clip, options, override_kind=override_kind, override_label=override_label)
    report["quality"] = spec.get("quality")
    return report


def validate_cq_override_args(args: argparse.Namespace) -> None:
    parse_quality_spec(getattr(args, "quality", None), option="--quality")
    main_title_cq = getattr(args, "main_title_cq", None)
    top_n_cq = getattr(args, "top_n_cq", None)
    main_title_mode = getattr(args, "main_title_bitrate_mode", None)
    top_n_mode = getattr(args, "top_n_bitrate_mode", None)
    main_title_quality = getattr(args, "main_title_quality", None)
    top_n_quality = getattr(args, "top_n_quality", None)
    main_requested = main_title_quality is not None or main_title_cq is not None or main_title_mode is not None
    top_requested = bool(top_n_quality) or bool(top_n_cq) or bool(top_n_mode)
    if main_requested and top_requested:
        raise ToolError("Main-title quality overrides cannot be used with top-N quality overrides")
    main_count = sum(1 for value in (main_title_quality, main_title_cq, main_title_mode) if value is not None)
    if main_count > 1:
        raise ToolError("Use only one main-title quality override")
    top_count = sum(1 for value in (top_n_quality, top_n_cq, top_n_mode) if bool(value))
    if top_count > 1:
        raise ToolError("Use only one top-N quality override")
    if main_title_quality is not None:
        parse_quality_spec(main_title_quality, option="--main-title-quality")
    if main_title_cq is not None:
        validate_cq_value(main_title_cq, option="--main-title-cq")
    if main_title_mode is not None:
        validate_bitrate_mode_value(main_title_mode, option="--main-title-bitrate-mode")
    if top_n_quality:
        parse_top_n_quality(top_n_quality, option="--top-n-quality")
    parse_top_n_cq(top_n_cq, option="--top-n-cq")
    parse_top_n_mode(top_n_mode, option="--top-n-bitrate-mode")

    clip_modes = flatten_clip_pairs(getattr(args, "clip_bitrate_mode", None))
    clip_cqs = flatten_clip_pairs(getattr(args, "clip_cq", None))
    clip_qualities = flatten_clip_pairs(getattr(args, "clip_quality", None))
    for _, mode in clip_modes:
        validate_bitrate_mode_value(mode, option="--clip-bitrate-mode MODE")
    for _, cq_value in clip_cqs:
        validate_cq_value(cq_value, option="--clip-cq CQ")
    for _, quality in clip_qualities:
        parse_quality_spec(quality, option="--clip-quality QUALITY")
    mode_names = {normalize_clip_name(clip) for clip, _ in clip_modes}
    cq_names = {normalize_clip_name(clip) for clip, _ in clip_cqs}
    quality_names = {normalize_clip_name(clip) for clip, _ in clip_qualities}
    copy_names = set(normalize_clip_names(getattr(args, "copy_clips", None)))
    duplicate_quality = sorted((mode_names & cq_names) | (mode_names & quality_names) | (cq_names & quality_names))
    if duplicate_quality:
        raise ToolError(f"Use only one quality override per clip: {', '.join(duplicate_quality)}")
    overridden_and_copied = sorted((mode_names | cq_names | quality_names) & copy_names)
    if overridden_and_copied:
        raise ToolError(f"Do not give both a quality override and --copy-clips for: {', '.join(overridden_and_copied)}")


def validate_encoder_bitrate_compatibility(args: argparse.Namespace) -> None:
    encoder = selected_hevc_encoder(args)
    options = bitrate_options_for_args(args)
    mode = normalize_bitrate_mode(str(options.get("mode") or "balanced"))
    if normalize_uhd_profile(getattr(args, "uhd_profile", "library")) == "disc":
        if not getattr(args, "target_disc_size", None):
            raise ToolError("--uhd-profile disc requires --target-disc-size bd25, bd50, bd66, bd100, or an explicit size.")
        if args_request_cq_quality(args, general_options=options) and not options.get("factor_override"):
            raise ToolError(
                "--uhd-profile disc requires predictable VBR sizing, so CQ/compact-cq is not allowed.\n"
                "Use --quality balanced, smaller, transparent, source-ratio:N, or codec-specific source ratios."
            )
    if encoder == "hevc_qsv" and args_request_cq_quality(args, general_options=options) and not options.get("factor_override"):
        raise ToolError(
            "compact-cq uses CQ rate control, but BD2HEVC does not currently support compact-cq with --encoder hevc_qsv.\n"
            "Use a CQ-capable encoder instead, for example:\n"
            "  python bd2hevc.py queue \"BD backups\" --output-dir \"Converted UHD-BD\" --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo --encoder libx265\n"
            "Or keep Intel QSV and choose a bitrate mode instead, for example:\n"
            "  python bd2hevc.py queue \"BD backups\" --output-dir \"Converted UHD-BD\" --bitrate-mode balanced --encoder hevc_qsv"
        )


def apply_general_quality_override(
    clips: list[dict[str, Any]],
    quality: Any,
    bitrate_options: dict[str, Any],
) -> dict[str, Any] | None:
    spec = parse_quality_spec(quality, option="--quality")
    if spec is None:
        return None
    selected = reencode_quality_candidates(clips)
    if spec.get("action") == "copy":
        reports = [
            copy_clip_for_quality_override(clip, override_kind="general_quality", override_label="general quality override to copy")
            for clip in selected
        ]
    else:
        reports = [
            retarget_clip(
                clip,
                quality_spec_bitrate_options(bitrate_options, spec),
                override_kind="general_quality",
                override_label=f"general quality override to {spec.get('quality')}",
            )
            for clip in selected
        ]
    return {"quality": spec.get("quality"), "clips": reports, "matched_count": len(reports)}


def apply_main_title_quality_override(
    clips: list[dict[str, Any]],
    quality: Any,
    bitrate_options: dict[str, Any] | None = None,
    feature_selection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    spec = parse_quality_spec(quality, option="--main-title-quality")
    if spec is None:
        return None
    candidates = main_feature_quality_candidates(clips, feature_selection)
    if not candidates:
        return None
    reports = [
        apply_quality_spec_to_clip(
            clip,
            spec,
            bitrate_options or {},
            override_kind="main_title_quality",
            override_label=f"main feature quality override to {spec.get('quality')}",
        )
        for clip in candidates
    ]
    return main_feature_override_report(feature_selection, reports, quality=spec.get("quality"))


def main_feature_quality_candidates(
    clips: list[dict[str, Any]],
    feature_selection: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidates = reencode_quality_candidates(clips)
    selected_ids = set((feature_selection or {}).get("clip_ids") or [])
    if selected_ids:
        selected = [
            clip for clip in candidates
            if Path(str(clip.get("file") or "")).stem in selected_ids
        ]
        if selected:
            return selected
    return candidates[:1]


def main_feature_override_report(
    feature_selection: dict[str, Any] | None,
    clips: list[dict[str, Any]],
    **fields: Any,
) -> dict[str, Any]:
    selection = feature_selection or {}
    report = {
        **fields,
        "selection": "playlist-feature" if selection.get("clip_ids") else "longest-physical-clip-fallback",
        "primary_playlist": selection.get("primary_playlist"),
        "playlists": [row.get("playlist") for row in selection.get("playlists") or []],
        "seamless_branching": bool(selection.get("seamless_branching")),
        "clips": clips,
        "matched_count": len(clips),
    }
    # Preserve the original single-clip report surface for callers that used
    # main-title overrides before playlist-aware feature selection existed.
    if len(clips) == 1:
        for key, value in clips[0].items():
            report.setdefault(key, value)
    return report


def apply_main_title_bitrate_mode_override(
    clips: list[dict[str, Any]],
    mode: str | None,
    bitrate_options: dict[str, Any] | None = None,
    feature_selection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if mode is None:
        return None
    candidates = main_feature_quality_candidates(clips, feature_selection)
    if not candidates:
        return None
    options = override_bitrate_options(bitrate_options, mode=mode)
    reports = [
        retarget_clip(
            clip,
            options,
            override_kind="main_title_quality",
            override_label=f"main feature bitrate mode override to {normalize_bitrate_mode(mode)}",
        )
        for clip in candidates
    ]
    return main_feature_override_report(feature_selection, reports, mode=normalize_bitrate_mode(mode))


def apply_main_title_cq_override(
    clips: list[dict[str, Any]],
    cq_value: int | None,
    bitrate_options: dict[str, Any] | None = None,
    feature_selection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if cq_value is None:
        return None
    cq_value = validate_cq_value(cq_value, option="--main-title-cq")
    candidates = main_feature_quality_candidates(clips, feature_selection)
    if not candidates:
        return None
    options = override_bitrate_options(bitrate_options, cq_value=cq_value)
    reports = []
    for clip in candidates:
        report = retarget_clip(clip, options, override_kind="main_title_cq", override_label=f"main feature CQ override to {cq_value}")
        report["cq"] = cq_value
        report["previous_cq"] = report["previous"].get("cq")
        reports.append(report)
    return main_feature_override_report(feature_selection, reports, cq=cq_value)


def apply_top_n_bitrate_mode_override(
    clips: list[dict[str, Any]],
    top_n_mode: list[Any] | tuple[Any, Any] | None,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    parsed = parse_top_n_mode(top_n_mode, option="--top-n-bitrate-mode")
    if parsed is None:
        return None
    count, mode = parsed
    selected = reencode_quality_candidates(clips)[:count]
    if not selected:
        return None
    options = override_bitrate_options(bitrate_options, mode=mode)
    report_clips = [
        retarget_clip(clip, options, override_kind="top_n_quality", override_label=f"top {count} bitrate mode override to {mode}")
        for clip in selected
    ]
    return {
        "count": count,
        "mode": mode,
        "clips": report_clips,
        "matched_count": len(report_clips),
    }


def apply_top_n_quality_override(
    clips: list[dict[str, Any]],
    top_n_quality: list[Any] | tuple[Any, Any] | None,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    parsed = parse_top_n_quality(top_n_quality, option="--top-n-quality")
    if parsed is None:
        return None
    count, spec = parsed
    selected = reencode_quality_candidates(clips)[:count]
    if not selected:
        return None
    reports = [
        apply_quality_spec_to_clip(
            clip,
            spec,
            bitrate_options or {},
            override_kind="top_n_quality",
            override_label=f"top {count} quality override to {spec.get('quality')}",
        )
        for clip in selected
    ]
    return {"count": count, "quality": spec.get("quality"), "clips": reports, "matched_count": len(reports)}


def apply_top_n_cq_override(clips: list[dict[str, Any]], top_n_cq: list[int] | tuple[int, int] | None) -> dict[str, Any] | None:
    return apply_top_n_cq_override_with_options(clips, top_n_cq, None)


def apply_top_n_cq_override_with_options(
    clips: list[dict[str, Any]],
    top_n_cq: list[int] | tuple[int, int] | None,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    parsed = parse_top_n_cq(top_n_cq, option="--top-n-cq")
    if parsed is None:
        return None
    count, cq_value = parsed
    selected = reencode_quality_candidates(clips)[:count]
    if not selected:
        return None
    options = override_bitrate_options(bitrate_options, cq_value=cq_value)
    report_clips = []
    for clip in selected:
        report = retarget_clip(clip, options, override_kind="top_n_cq", override_label=f"top {count} CQ override to {cq_value}")
        report["previous_cq"] = report["previous"].get("cq")
        report["cq"] = cq_value
        report_clips.append(report)
    return {
        "count": count,
        "cq": cq_value,
        "clips": report_clips,
        "matched_count": len(report_clips),
    }


def apply_named_clip_quality_overrides(
    clips: list[dict[str, Any]],
    bitrate_options: dict[str, Any],
    *,
    clip_quality: Any = None,
    clip_bitrate_mode: Any = None,
    clip_cq: Any = None,
) -> dict[str, Any] | None:
    clip_qualities = flatten_clip_pairs(clip_quality)
    clip_modes = flatten_clip_pairs(clip_bitrate_mode)
    clip_cqs = flatten_clip_pairs(clip_cq)
    if not clip_qualities and not clip_modes and not clip_cqs:
        return None
    reports: list[dict[str, Any]] = []
    all_names = [normalize_clip_name(clip) for clip, _ in clip_qualities + clip_modes + clip_cqs]
    require_named_clips(clips, all_names, option="clip quality override")
    lookup = clip_lookup_by_name(clips)
    for clip_name, quality_value in clip_qualities:
        name = normalize_clip_name(clip_name)
        clip = lookup[name]
        if not clip_is_reencode_eligible(clip):
            raise ToolError(f"--clip-quality {name} has no effect because that clip action is {clip.get('action')}")
        spec = parse_quality_spec(quality_value, option="--clip-quality QUALITY")
        if spec is None:
            raise ToolError("--clip-quality QUALITY cannot be empty")
        report = apply_quality_spec_to_clip(
            clip,
            spec,
            bitrate_options,
            override_kind="clip_quality",
            override_label=f"clip quality override to {spec.get('quality')}",
        )
        report["selector"] = name
        reports.append(report)
    for clip_name, mode_value in clip_modes:
        name = normalize_clip_name(clip_name)
        clip = lookup[name]
        if not clip_is_reencode_eligible(clip):
            raise ToolError(f"--clip-bitrate-mode {name} has no effect because that clip action is {clip.get('action')}")
        mode = validate_bitrate_mode_value(mode_value, option="--clip-bitrate-mode MODE")
        options = override_bitrate_options(bitrate_options, mode=mode)
        report = retarget_clip(clip, options, override_kind="clip_quality", override_label=f"clip bitrate mode override to {mode}")
        report["selector"] = name
        report["mode"] = mode
        reports.append(report)
    for clip_name, cq_value_raw in clip_cqs:
        name = normalize_clip_name(clip_name)
        clip = lookup[name]
        if not clip_is_reencode_eligible(clip):
            raise ToolError(f"--clip-cq {name} has no effect because that clip action is {clip.get('action')}")
        cq_value = validate_cq_value(cq_value_raw, option="--clip-cq CQ")
        options = override_bitrate_options(bitrate_options, cq_value=cq_value)
        report = retarget_clip(clip, options, override_kind="clip_cq", override_label=f"clip CQ override to {cq_value}")
        report["selector"] = name
        report["previous_cq"] = report["previous"].get("cq")
        report["cq"] = cq_value
        reports.append(report)
    return {"clips": reports, "matched_count": len(reports)}


def apply_clip_copy_overrides(clips: list[dict[str, Any]], requested: Any) -> dict[str, Any] | None:
    names = normalize_clip_names(requested)
    if not names:
        return None
    selected = require_named_clips(clips, names, option="--copy-clips")
    report_clips: list[dict[str, Any]] = []
    for name, clip in zip(names, selected):
        previous_action = clip.get("action")
        if previous_action == "reencode":
            clip["action"] = "copy"
            clip["copy_override"] = True
            clip["copy_override_reason"] = "requested by --copy-clips/--exclude-clips"
        report_clips.append(
            {
                "file": clip.get("file") or name,
                "duration": clip.get("duration"),
                "previous_action": previous_action,
                "action": clip.get("action"),
            }
        )
    return {"requested": names, "clips": report_clips, "matched_count": len(report_clips)}


def source_field_order(video: dict[str, Any]) -> str:
    return str(video.get("field_order") or "").strip().lower()


def clip_is_interlaced_by_metadata(clip: dict[str, Any]) -> bool:
    video = clip.get("video") or {}
    return source_field_order(video) in INTERLACED_FIELD_ORDERS


def apply_deinterlace_plan(clips: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any] | None:
    mode = getattr(args, "deinterlace", DEFAULT_DEINTERLACE_MODE)
    force_names = set(normalize_clip_names(getattr(args, "deinterlace_clips", None)))
    skip_names = set(normalize_clip_names(getattr(args, "no_deinterlace_clips", None)))
    if force_names:
        require_named_clips(clips, sorted(force_names), option="--deinterlace-clips")
    if skip_names:
        require_named_clips(clips, sorted(skip_names), option="--no-deinterlace-clips")
    if mode == "off" and not force_names:
        return None
    selected: list[dict[str, Any]] = []
    for clip in clips:
        name = normalize_clip_name(clip.get("file") or "")
        video = clip.get("video") or {}
        reason = None
        if name in skip_names:
            reason = None
        elif name in force_names:
            reason = "requested by --deinterlace-clips"
        elif mode == "force":
            reason = "requested by --deinterlace force"
        elif mode == "auto" and clip_is_interlaced_by_metadata(clip):
            reason = f"source field_order={video.get('field_order')}"
        if not reason:
            continue
        if clip.get("action") != "reencode":
            selected.append(
                {
                    "file": clip.get("file"),
                    "action": clip.get("action"),
                    "field_order": video.get("field_order"),
                    "selected": False,
                    "reason": f"deinterlace skipped because clip action is {clip.get('action')}",
                }
            )
            continue
        postprocess = video.setdefault("postprocess", {})
        postprocess["deinterlace"] = {
            "enabled": True,
            "mode": mode,
            "filter": getattr(args, "deinterlace_filter", "bwdif"),
            "reason": reason,
        }
        selected.append(
            {
                "file": clip.get("file"),
                "action": clip.get("action"),
                "field_order": video.get("field_order"),
                "selected": True,
                "filter": getattr(args, "deinterlace_filter", "bwdif"),
                "reason": reason,
            }
        )
    return {
        "mode": mode,
        "filter": getattr(args, "deinterlace_filter", "bwdif"),
        "force_clips": sorted(force_names),
        "skip_clips": sorted(skip_names),
        "clips": selected,
        "matched_count": sum(1 for item in selected if item.get("selected")),
    }


def apply_quality_overrides(
    clips: list[dict[str, Any]],
    bitrate_options: dict[str, Any],
    args: argparse.Namespace,
    feature_selection: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    general_quality_override = apply_general_quality_override(clips, getattr(args, "quality", None), bitrate_options)
    main_title_quality_override = None
    main_title_cq_override = None
    main_title_mode_override = None
    top_n_quality_override = None
    top_n_cq_override = None
    top_n_mode_override = None
    if getattr(args, "main_title_quality", None) is not None:
        main_title_quality_override = apply_main_title_quality_override(clips, getattr(args, "main_title_quality", None), bitrate_options, feature_selection)
    elif getattr(args, "main_title_cq", None) is not None:
        main_title_cq_override = apply_main_title_cq_override(clips, getattr(args, "main_title_cq", None), bitrate_options, feature_selection)
    elif getattr(args, "main_title_bitrate_mode", None) is not None:
        main_title_mode_override = apply_main_title_bitrate_mode_override(clips, getattr(args, "main_title_bitrate_mode", None), bitrate_options, feature_selection)
    if getattr(args, "top_n_quality", None):
        top_n_quality_override = apply_top_n_quality_override(clips, getattr(args, "top_n_quality", None), bitrate_options)
    elif getattr(args, "top_n_cq", None):
        top_n_cq_override = apply_top_n_cq_override_with_options(clips, getattr(args, "top_n_cq", None), bitrate_options)
    elif getattr(args, "top_n_bitrate_mode", None):
        top_n_mode_override = apply_top_n_bitrate_mode_override(clips, getattr(args, "top_n_bitrate_mode", None), bitrate_options)
    named_clip_overrides = apply_named_clip_quality_overrides(
        clips,
        bitrate_options,
        clip_quality=getattr(args, "clip_quality", None),
        clip_bitrate_mode=getattr(args, "clip_bitrate_mode", None),
        clip_cq=getattr(args, "clip_cq", None),
    )
    report = {
        "general": general_quality_override,
        "main_title_quality": main_title_quality_override,
        "main_title_cq": main_title_cq_override,
        "main_title_bitrate_mode": main_title_mode_override,
        "top_n_quality": top_n_quality_override,
        "top_n_cq": top_n_cq_override,
        "top_n_bitrate_mode": top_n_mode_override,
        "named_clips": named_clip_overrides,
    }
    return report if any(value is not None for value in report.values()) else None


def source_bitrate_files_for_effective_plan(
    clips: list[dict[str, Any]],
    bitrate_options: dict[str, Any],
    args: argparse.Namespace,
    feature_selection: dict[str, Any] | None = None,
) -> set[str]:
    preview = copy.deepcopy(clips)
    remember_original_clip_actions(preview)
    apply_quality_overrides(preview, bitrate_options, args, feature_selection)
    apply_clip_copy_overrides(preview, getattr(args, "copy_clips", None))
    return {
        str(clip.get("file") or "")
        for clip in preview
        if clip.get("action") == "reencode"
        and (clip.get("video") or {}).get("target_hevc", {}).get("rate_control") != "cq"
    }


def scan_clone_source_for_plan(
    source: Path,
    tools: dict[str, Any],
    args: argparse.Namespace,
    bitrate_options: dict[str, Any],
) -> dict[str, Any]:
    from . import planning_workflow
    return planning_workflow.scan_clone_source_for_plan(source, tools, args, bitrate_options, services=sys.modules[__name__])


def extract_title_with_makemkv(source: Path, title_id: int, destination: Path, tools: dict[str, Any], *, dry_run: bool, verbose: bool) -> Path:
    exe = tools.get("makemkvcon64") or tools.get("makemkvcon")
    if not exe:
        raise ToolError("MakeMKV CLI is required for multi-segment title extraction")
    cmd = [str(exe), "mkv", f"file:{source}", str(title_id), str(destination)]
    if dry_run:
        return destination / f"title_{title_id:02d}.mkv"
    destination.mkdir(parents=True, exist_ok=True)
    before = set(destination.glob("*.mkv"))
    run_cmd(cmd, check=True, capture=False, verbose=verbose)
    after = set(destination.glob("*.mkv"))
    created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
    if not created:
        created = sorted(destination.glob("*.mkv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not created:
        raise ToolError("MakeMKV did not create an MKV output")
    return created[0]


def convert_movie_only(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if args.dry_run:
        return _convert_movie_only_in_place(args, tools)
    source = Path(args.source).resolve()
    destination = Path(args.output).resolve() if args.output else default_output_for(source, "movie-only")
    validate_output_available(destination, source, force=args.force)
    destination.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    staging = destination.with_name(f".{destination.name}.conversion-{uuid.uuid4().hex}")
    staged_args = copy.copy(args)
    staged_args.output, staged_args.force = str(staging), False
    try:
        result = _convert_movie_only_in_place(staged_args, tools)
        if not conversion_succeeded(result, require_makemkv=getattr(args, "require_makemkv", False)):
            raise ToolError("Movie-only replacement failed validation; previous output has been preserved")
        swap_directory(staging, destination, force=args.force)
        def relocate(value):
            if isinstance(value, str): return value.replace(str(staging), str(destination))
            if isinstance(value, list): return [relocate(item) for item in value]
            if isinstance(value, dict): return {key:relocate(item) for key,item in value.items()}
            return value
        return relocate(result)
    finally:
        if staging.exists() and staging.parent == destination.parent:
            shutil.rmtree(staging)


def _convert_movie_only_in_place(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if getattr(args, "no_makemkv", False):
        raise ToolError("movie-only mode uses MakeMKV title selection; use auto/clone-streams for MakeMKV-free conversion")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve() if args.output else default_output_for(source, "movie-only")
    if not args.dry_run:
        make_output_available(output, source, force=args.force)
    makemkv = run_makemkv_scan(source, tools, verbose=args.verbose)
    title = choose_title(makemkv, args.title)
    if args.staging_dir:
        staging_root = Path(args.staging_dir).resolve()
        from .runtime_support import reject_overlap
        try:
            reject_overlap(staging_root, [source, output])
        except ValueError as error:
            raise ToolError(str(error)) from error
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="bd2hevc-movie-", dir=str(staging_root)))
    else:
        staging = Path(tempfile.mkdtemp(prefix=f"{source.name}_bd2uhd_", dir=str(output.parent)))
    staging.mkdir(parents=True, exist_ok=True)
    if args.sample_seconds and args.sample_start:
        raise ToolError("Movie-only sample authoring uses split sources, so --sample-start must be 0 for aligned audio/subtitles.")

    source_clip = clip_path_for_title(source, title)
    extracted_mkv: Path | None = None
    if not source_clip or not source_clip.exists() or args.extract_with_makemkv:
        extracted_mkv = extract_title_with_makemkv(source, int(title["id"]), staging, tools, dry_run=args.dry_run, verbose=args.verbose)
        transcode_input = extracted_mkv
    else:
        transcode_input = source_clip

    bitrate_options = bitrate_options_for_args(args)
    clip_info = inspect_clip(
        transcode_input,
        tools,
        accurate_video_bitrate=not args.fast_bitrate,
        depad_video_padding=depad_video_padding_from_args(args),
        bitrate_options=bitrate_options,
    )
    if clip_info.get("action") != "reencode" and not args.force_encode:
        raise ToolError(f"Selected title does not require non-HEVC reencoding: {transcode_input}")
    postprocess = apply_deinterlace_plan([clip_info], args)
    mux_clip_info = copy.deepcopy(clip_info)
    if args.uhd_scale:
        video_info = mux_clip_info.setdefault("video", {})
        video_info["width"] = 3840
        video_info["height"] = 2160
    encoded = staging / f"{source.name}_title{int(title['id']):02d}_video.hevc"
    ffmpeg_cmd = encode_to_hevc_m2ts(
        transcode_input,
        encoded,
        clip_info,
        tools,
        sample_seconds=args.sample_seconds,
        sample_start=args.sample_start,
        video_only=True,
        scale_uhd=args.uhd_scale,
        hevc_bit_depth=args.hevc_bit_depth,
        encoder=selected_hevc_encoder(args),
        deinterlace_filter=getattr(args, "deinterlace_filter", "bwdif"),
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    if args.dry_run:
        return {
            "mode": "movie-only",
            "source": str(source),
            "output": str(output),
            "selected_title": title_summary(title),
            "transcode_input": str(transcode_input),
            "staging": str(staging),
            "clip_info": clip_summary(clip_info),
            "encoder": selected_hevc_encoder(args),
            "bitrate": bitrate_options,
            "postprocess": postprocess,
            "uhd_scale": args.uhd_scale,
            "skip_audio": args.skip_audio,
            "skip_subtitles": args.skip_subtitles,
            "ffmpeg_command": format_cmd(ffmpeg_cmd),
        }
    authored_meta = author_uhdbd_split(
        encoded,
        transcode_input,
        output,
        mux_clip_info,
        tools,
        sample_seconds=args.sample_seconds,
        sample_start=args.sample_start,
        include_audio=not args.skip_audio,
        include_subtitles=not args.skip_subtitles,
        dry_run=False,
        verbose=args.verbose,
    )
    disc_metadata = ensure_disc_library_metadata(output)
    output_clip = output / "BDMV" / "STREAM" / "00000.m2ts"
    validation_source = transcode_input
    validation = validate_clip(
        validation_source,
        output_clip,
        tools,
        decode_seconds=args.decode_sample,
        require_hevc="always",
        expected_video=mux_clip_info.get("video"),
        preserve_subtitles=not args.skip_subtitles,
        preserve_audio=not args.skip_audio,
        expected_duration=args.sample_seconds,
        preserve_stream_ids=False,
    )
    makemkv_validation = None
    if not args.sample_seconds:
        makemkv_validation = validate_disc_titles(
            output,
            tools,
            expected_duration=title.get("duration"),
            verbose=args.verbose,
            use_makemkv=use_makemkv_from_args(args),
            require_makemkv=getattr(args, "require_makemkv", False),
        )
    if not args.keep_staging:
        shutil.rmtree(staging, ignore_errors=True)
    result = {
        "mode": "movie-only",
        "source": str(source),
        "output": str(output),
        "selected_title": title_summary(title),
        "uhd_scale": args.uhd_scale,
        "skip_audio": args.skip_audio,
        "skip_subtitles": args.skip_subtitles,
        "encoded_intermediate": str(encoded),
        "meta": str(authored_meta),
        "disc_metadata": disc_metadata,
        "validation": validation,
    }
    if makemkv_validation is not None:
        result["makemkv_validation"] = makemkv_validation
    return result


def clone_clip_context(source: Path, output: Path, clip: dict[str, Any]) -> dict[str, Any]:
    input_path = Path(clip["path"])
    output_path = output / "BDMV" / "STREAM" / clip["file"]
    output_clpi = output / "BDMV" / "CLIPINF" / f"{Path(clip['file']).stem}.clpi"
    backup_clpi = output / "BDMV" / "BACKUP" / "CLIPINF" / f"{Path(clip['file']).stem}.clpi"
    temp_video = output_path.with_suffix(".hevc.tmp")
    temp_audio_prefix = output_path.with_suffix(".compact-audio")
    return {
        "clip": clip,
        "file": clip["file"],
        "input_path": input_path,
        "output_path": output_path,
        "output_clpi": output_clpi,
        "backup_clpi": backup_clpi,
        "temp_video": temp_video,
        "temp_audio_prefix": temp_audio_prefix,
    }


def clone_streams_plan_payload(
    source: Path,
    output: Path,
    args: argparse.Namespace,
    bitrate_options: dict[str, Any],
    main_title_cq_override: dict[str, Any] | None,
    top_n_cq_override: dict[str, Any] | None,
    clips: list[dict[str, Any]],
    *,
    compact_audio_remux_clips: list[dict[str, Any]] | None = None,
    quality_overrides: dict[str, Any] | None = None,
    copy_clip_overrides: dict[str, Any] | None = None,
    postprocess: dict[str, Any] | None = None,
    target_disc_fit: dict[str, Any] | None = None,
    planning: dict[str, Any] | None = None,
    planning_pending: bool = False,
) -> dict[str, Any]:
    from . import planning_workflow
    return planning_workflow.clone_streams_plan_payload(source, output, args, bitrate_options, main_title_cq_override, top_n_cq_override, clips, compact_audio_remux_clips=compact_audio_remux_clips, quality_overrides=quality_overrides, copy_clip_overrides=copy_clip_overrides, postprocess=postprocess, target_disc_fit=target_disc_fit, planning=planning, planning_pending=planning_pending, services=sys.modules[__name__])


def encode_clone_clip_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    progress_event("encode-start", ctx["file"])
    try:
        encode_to_hevc_m2ts(
            ctx["input_path"],
            ctx["temp_video"],
            ctx["clip"],
            tools,
            video_only=True,
            hevc_bit_depth=args.hevc_bit_depth,
            encoder=selected_hevc_encoder(args),
            deinterlace_filter=getattr(args, "deinterlace_filter", "bwdif"),
            dry_run=False,
            verbose=args.verbose,
        )
    except Exception:
        progress_event("encode-failed", ctx["file"])
        raise
    progress_event("encode-done", ctx["file"])
    return ctx


def transcode_compact_audio_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    progress_event("audio-start", ctx["file"])
    try:
        audio_tracks, _audio_cmd = transcode_compact_audio_tracks(
            ctx["input_path"],
            ctx["temp_audio_prefix"],
            ctx["clip"],
            tools,
            stereo_audio_bitrate=stereo_audio_bitrate_from_args(args),
            mono_audio_bitrate=mono_audio_bitrate_from_args(args),
            dry_run=False,
            verbose=args.verbose,
        )
        ctx["compact_audio_tracks"] = audio_tracks
    except Exception:
        progress_event("audio-failed", ctx["file"])
        raise
    progress_event("audio-done", ctx["file"])
    return ctx


def mux_validate_clone_clip_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    compact_audio = audio_mode_from_args(args) == "compact-stereo"
    progress_event("mux-start", ctx["file"])
    try:
        author_m2ts_split(
            ctx["temp_video"],
            ctx["input_path"],
            ctx["output_path"],
            ctx["clip"],
            tools,
            compact_audio_tracks=ctx.get("compact_audio_tracks") if compact_audio else None,
            reference_clip_info=ctx["clip"],
            verbose=args.verbose,
        )
    except Exception:
        progress_event("mux-failed", ctx["file"])
        raise
    progress_event("mux-done", ctx["file"])
    clpi_report = restore_source_clpi(
        ctx["input_path"],
        ctx["output_clpi"],
        output_clip=ctx["output_path"],
        patch_version_headers=patch_version_headers_from_args(args),
        tools=tools,
        compact_audio=compact_audio,
    )
    if clpi_report.get("restored") and ctx["output_clpi"].exists():
        ctx["backup_clpi"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ctx["output_clpi"], ctx["backup_clpi"])
    ctx["temp_video"].unlink(missing_ok=True)
    for audio in ctx.get("compact_audio_tracks") or []:
        Path(str(audio.get("path") or "")).unlink(missing_ok=True)
    validation = validate_clip(
        ctx["input_path"],
        ctx["output_path"],
        tools,
        decode_seconds=args.decode_sample,
        require_hevc="always",
        audio_mode=audio_mode_from_args(args),
    )
    progress_event("validate-done", ctx["file"])
    return validation


def finalize_clone_clip_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if audio_mode_from_args(args) == "compact-stereo":
        transcode_compact_audio_context(ctx, tools, args)
    return mux_validate_clone_clip_context(ctx, tools, args)


def clip_needs_compact_audio_remux(clip: dict[str, Any]) -> bool:
    return clip.get("action") != "reencode" and bool(clip.get("video")) and bool(compact_audio_source_streams(clip))


def compact_audio_stream_matches_target(
    audio: dict[str, Any],
    *,
    stereo_audio_bitrate: int,
    mono_audio_bitrate: int,
) -> bool:
    channels = safe_int(audio.get("channels")) or 0
    expected_channels = 1 if channels == 1 else 2
    expected_bitrate = mono_audio_bitrate if expected_channels == 1 else stereo_audio_bitrate
    bitrate = safe_int(audio.get("bit_rate"))
    return (
        audio.get("codec_name") == "ac3"
        and channels == expected_channels
        and (bitrate is None or bitrate == expected_bitrate)
    )


def compact_audio_repair_reasons(
    clip: dict[str, Any],
    *,
    stereo_audio_bitrate: int,
    mono_audio_bitrate: int,
) -> list[str]:
    if not clip.get("video"):
        return []
    reasons: list[str] = []
    for index, audio in enumerate(compact_audio_source_streams(clip)):
        if compact_audio_stream_matches_target(
            audio,
            stereo_audio_bitrate=stereo_audio_bitrate,
            mono_audio_bitrate=mono_audio_bitrate,
        ):
            continue
        codec = audio.get("codec_name") or "unknown"
        channels = safe_int(audio.get("channels")) or 0
        bitrate = safe_int(audio.get("bit_rate"))
        detail = f"audio {index + 1}: {codec}, {channels} channel(s)"
        if bitrate:
            detail += f", {bitrate} bps"
        reasons.append(detail)
    return reasons


def remux_compact_audio_copy_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    tracks = parse_tsmuxer_tracks(ctx["input_path"], tools, verbose=args.verbose)
    video_track = next((track for track in tracks if str(track.get("stream_id") or "").startswith("V_")), None)
    if not video_track:
        raise ToolError(f"tsMuxeR did not expose a video track for {ctx['input_path']}")
    temp_output = ctx["output_path"].with_name(f"{ctx['output_path'].stem}.compact-remux.tmp.m2ts")
    original_temp = ctx["output_path"].with_name(f"{ctx['output_path'].stem}.precompact.tmp.m2ts")
    in_place = ctx["input_path"].resolve() == ctx["output_path"].resolve()
    meta_path = temp_output.with_suffix(".meta")
    audio_tracks: list[dict[str, Any]] = []
    in_place_committed = False
    temp_output.unlink(missing_ok=True)
    if in_place:
        original_temp.unlink(missing_ok=True)
    try:
        audio_tracks, _audio_cmd = transcode_compact_audio_tracks(
            ctx["input_path"],
            ctx["temp_audio_prefix"],
            ctx["clip"],
            tools,
            stereo_audio_bitrate=stereo_audio_bitrate_from_args(args),
            mono_audio_bitrate=mono_audio_bitrate_from_args(args),
            dry_run=False,
            verbose=args.verbose,
        )
        author_m2ts_split(
            ctx["input_path"],
            ctx["input_path"],
            temp_output,
            ctx["clip"],
            tools,
            video_track_id=video_track.get("track"),
            video_stream_id=str(video_track.get("stream_id") or ""),
            compact_audio_tracks=audio_tracks,
            reference_clip_info=ctx["clip"],
            verbose=args.verbose,
        )
        validation = validate_clip(
            ctx["input_path"],
            temp_output,
            tools,
            decode_seconds=args.decode_sample,
            require_hevc="never",
            audio_mode="compact-stereo",
        )
        source_codec = (ctx["clip"].get("video") or {}).get("codec_name")
        output_probe = validation.get("output_probe") or {}
        output_codec = (output_probe.get("video") or {}).get("codec_name")
        validation["checks"].append(
            {
                "name": "video_codec_passthrough",
                "ok": source_codec == output_codec,
                "source": source_codec,
                "output": output_codec,
            }
        )
        source_subtitles = [subtitle.get("codec_name") for subtitle in ctx["clip"].get("subtitles", [])]
        output_subtitles = [subtitle.get("codec_name") for subtitle in output_probe.get("subtitles", [])]
        validation["checks"].append(
            {
                "name": "subtitle_codecs_passthrough",
                "ok": source_subtitles == output_subtitles,
                "source": source_subtitles,
                "output": output_subtitles,
            }
        )
        source_duration = safe_float(ctx["clip"].get("duration"))
        output_duration = safe_float(output_probe.get("duration"))
        if source_duration is not None and output_duration is not None:
            duration_tolerance = max(0.5, min(2.0, source_duration * 0.005))
            validation["checks"].append(
                {
                    "name": "audio_remux_duration_matches_source",
                    "ok": abs(source_duration - output_duration) <= duration_tolerance,
                    "source_duration": source_duration,
                    "output_duration": output_duration,
                    "tolerance_seconds": duration_tolerance,
                }
            )
        validation["ok"] = all(bool(check.get("ok")) for check in validation.get("checks", []))
        if not validation["ok"]:
            raise ToolError(f"Compact-audio remux validation failed for {ctx['file']}")
        if in_place:
            clpi_bytes = ctx["output_clpi"].read_bytes() if ctx["output_clpi"].exists() else None
            backup_clpi_bytes = ctx["backup_clpi"].read_bytes() if ctx["backup_clpi"].exists() else None
            ctx["input_path"].replace(original_temp)
            try:
                temp_output.replace(ctx["output_path"])
                patch_report = patch_clpi_for_output(
                    ctx["output_clpi"],
                    patch_video_to_hevc=source_codec == "hevc",
                    compact_audio=True,
                    patch_version_headers=patch_version_headers_from_args(args),
                )
                cpi_scale = scale_clpi_cpi_map_to_stream(
                    original_temp,
                    ctx["output_path"],
                    ctx["output_clpi"],
                    tools=tools,
                )
                clpi_report = {
                    "output_clpi": str(ctx["output_clpi"]),
                    "restored": False,
                    "patched_in_place": True,
                    "patch": patch_report,
                    "cpi_scale": cpi_scale,
                }
                if ctx["output_clpi"].exists():
                    ctx["backup_clpi"].parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ctx["output_clpi"], ctx["backup_clpi"])
                in_place_committed = True
            except Exception:
                ctx["output_path"].unlink(missing_ok=True)
                if original_temp.exists():
                    original_temp.replace(ctx["output_path"])
                if clpi_bytes is None:
                    ctx["output_clpi"].unlink(missing_ok=True)
                else:
                    ctx["output_clpi"].write_bytes(clpi_bytes)
                if backup_clpi_bytes is None:
                    ctx["backup_clpi"].unlink(missing_ok=True)
                else:
                    ctx["backup_clpi"].parent.mkdir(parents=True, exist_ok=True)
                    ctx["backup_clpi"].write_bytes(backup_clpi_bytes)
                raise
        else:
            temp_output.replace(ctx["output_path"])
            clpi_report = restore_source_clpi(
                ctx["input_path"],
                ctx["output_clpi"],
                output_clip=ctx["output_path"],
                patch_version_headers=patch_version_headers_from_args(args),
                tools=tools,
                patch_video_to_hevc=False,
                compact_audio=True,
            )
        if clpi_report.get("restored") and ctx["output_clpi"].exists():
            ctx["backup_clpi"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ctx["output_clpi"], ctx["backup_clpi"])
        validation["output"] = str(ctx["output_path"])
        validation["compact_audio_remux"] = True
        validation["clpi"] = clpi_report
        return validation
    finally:
        temp_output.unlink(missing_ok=True)
        if in_place_committed:
            original_temp.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        for audio in audio_tracks:
            Path(str(audio.get("path") or "")).unlink(missing_ok=True)


def run_queued_encode_mux_pipeline(
    contexts: list[dict[str, Any]],
    tools: dict[str, Any],
    args: argparse.Namespace,
    *,
    total_seconds: float,
    progress_enabled: bool,
) -> tuple[list[dict[str, Any]], float]:
    validations: list[dict[str, Any]] = []
    done_seconds = 0.0
    max_depth = max(1, int(getattr(args, "encode_ahead_depth", 3) or 1))
    encoded_queue: thread_queue.Queue[tuple[str, dict[str, Any] | BaseException | None]] = thread_queue.Queue(maxsize=max_depth)
    stop_event = threading.Event()

    def queue_put(item: tuple[str, dict[str, Any] | BaseException | None]) -> bool:
        while not stop_event.is_set():
            try:
                encoded_queue.put(item, timeout=0.5)
                return True
            except thread_queue.Full:
                continue
        return False

    def producer() -> None:
        try:
            for ctx in contexts:
                if stop_event.is_set():
                    break
                encode_clone_clip_context(ctx, tools, args)
                if not queue_put(("encoded", ctx)):
                    break
        except BaseException as exc:
            queue_put(("error", exc))
        finally:
            queue_put(("done", None))

    progress_event("pipeline", "enabled", mode="encode-mux-queue", depth=max_depth)
    with ThreadPoolExecutor(max_workers=1) as executor:
        producer_future = executor.submit(producer)
        try:
            while True:
                kind, payload = encoded_queue.get()
                if kind == "done":
                    producer_future.result()
                    break
                if kind == "error":
                    producer_future.result()
                    raise payload  # type: ignore[misc]
                ctx = payload
                assert isinstance(ctx, dict)
                emit_conversion_progress(
                    done_seconds,
                    total_seconds,
                    len(validations),
                    len(contexts),
                    current=ctx["file"],
                    stage="muxing encoded queue",
                    enabled=progress_enabled,
                )
                validation = finalize_clone_clip_context(ctx, tools, args)
                validations.append(validation)
                done_seconds += float(ctx["clip"].get("duration") or 0)
                emit_conversion_progress(
                    done_seconds,
                    total_seconds,
                    len(validations),
                    len(contexts),
                    current=ctx["file"],
                    stage="validated",
                    enabled=progress_enabled,
                )
        except BaseException:
            stop_event.set()
            while True:
                try:
                    encoded_queue.get_nowait()
                except thread_queue.Empty:
                    break
            raise
    return validations, done_seconds


def run_queued_encode_audio_mux_pipeline(
    contexts: list[dict[str, Any]],
    tools: dict[str, Any],
    args: argparse.Namespace,
    *,
    total_seconds: float,
    progress_enabled: bool,
) -> tuple[list[dict[str, Any]], float]:
    validations: list[dict[str, Any]] = []
    done_seconds = 0.0
    max_depth = max(1, int(getattr(args, "encode_ahead_depth", 3) or 1))
    result_queue: thread_queue.Queue[tuple[str, dict[str, Any] | BaseException | None]] = thread_queue.Queue(maxsize=max_depth * 2 + 2)
    stop_event = threading.Event()
    lane_condition = threading.Condition()
    pending_by_lane = {"video": 0, "audio": 0}

    def queue_put(item: tuple[str, dict[str, Any] | BaseException | None]) -> bool:
        while not stop_event.is_set():
            try:
                result_queue.put(item, timeout=0.5)
                return True
            except thread_queue.Full:
                continue
        return False

    def wait_for_lane_room(lane: str) -> bool:
        with lane_condition:
            while not stop_event.is_set() and pending_by_lane[lane] >= max_depth:
                lane_condition.wait(timeout=0.5)
            return not stop_event.is_set()

    def mark_lane_ready(lane: str) -> None:
        with lane_condition:
            pending_by_lane[lane] += 1
            lane_condition.notify_all()

    def release_lane_outputs() -> None:
        with lane_condition:
            pending_by_lane["video"] = max(0, pending_by_lane["video"] - 1)
            pending_by_lane["audio"] = max(0, pending_by_lane["audio"] - 1)
            lane_condition.notify_all()

    def video_worker() -> None:
        try:
            for ctx in contexts:
                if stop_event.is_set():
                    break
                if not wait_for_lane_room("video"):
                    break
                encode_clone_clip_context(ctx, tools, args)
                mark_lane_ready("video")
                if not queue_put(("video", ctx)):
                    break
        except BaseException as exc:
            queue_put(("error", exc))
        finally:
            queue_put(("video-done", None))

    def audio_worker() -> None:
        try:
            for ctx in contexts:
                if stop_event.is_set():
                    break
                if not wait_for_lane_room("audio"):
                    break
                transcode_compact_audio_context(ctx, tools, args)
                mark_lane_ready("audio")
                if not queue_put(("audio", ctx)):
                    break
        except BaseException as exc:
            queue_put(("error", exc))
        finally:
            queue_put(("audio-done", None))

    def stop_workers() -> None:
        stop_event.set()
        with lane_condition:
            lane_condition.notify_all()

    progress_event("pipeline", "enabled", mode="video-audio-mux-queue", depth=max_depth)
    with ThreadPoolExecutor(max_workers=2) as executor:
        video_future = executor.submit(video_worker)
        audio_future = executor.submit(audio_worker)
        video_ready: dict[str, dict[str, Any]] = {}
        audio_ready: dict[str, dict[str, Any]] = {}
        mux_queue: list[dict[str, Any]] = []
        queued_for_mux: set[str] = set()
        muxed: set[str] = set()
        video_done = False
        audio_done = False

        def queue_mux_if_ready(clip_file: str) -> None:
            if clip_file in queued_for_mux or clip_file in muxed:
                return
            if clip_file in video_ready and clip_file in audio_ready:
                mux_queue.append(video_ready[clip_file])
                queued_for_mux.add(clip_file)

        try:
            while True:
                if mux_queue:
                    ctx = mux_queue.pop(0)
                    clip_file = ctx["file"]
                    emit_conversion_progress(
                        done_seconds,
                        total_seconds,
                        len(validations),
                        len(contexts),
                        current=clip_file,
                        stage="muxing video/audio queue",
                        enabled=progress_enabled,
                    )
                    validation = mux_validate_clone_clip_context(ctx, tools, args)
                    validations.append(validation)
                    done_seconds += float(ctx["clip"].get("duration") or 0)
                    muxed.add(clip_file)
                    video_ready.pop(clip_file, None)
                    audio_ready.pop(clip_file, None)
                    release_lane_outputs()
                    emit_conversion_progress(
                        done_seconds,
                        total_seconds,
                        len(validations),
                        len(contexts),
                        current=clip_file,
                        stage="validated",
                        enabled=progress_enabled,
                    )
                    continue
                if video_done and audio_done and len(muxed) >= len(contexts):
                    audio_future.result()
                    video_future.result()
                    break
                kind, payload = result_queue.get()
                if kind == "error":
                    stop_workers()
                    for future in (audio_future, video_future):
                        try:
                            future.result()
                        except BaseException:
                            pass
                    raise payload  # type: ignore[misc]
                if kind == "video-done":
                    video_done = True
                    video_future.result()
                    continue
                if kind == "audio-done":
                    audio_done = True
                    audio_future.result()
                    continue
                ctx = payload
                assert isinstance(ctx, dict)
                if kind == "video":
                    video_ready[ctx["file"]] = ctx
                    queue_mux_if_ready(ctx["file"])
                elif kind == "audio":
                    audio_ready[ctx["file"]] = ctx
                    queue_mux_if_ready(ctx["file"])
        except BaseException:
            stop_workers()
            while True:
                try:
                    result_queue.get_nowait()
                except thread_queue.Empty:
                    break
            raise
    return validations, done_seconds


def convert_clone_streams(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if args.dry_run:
        return _convert_clone_streams_in_place(args, tools)
    source = Path(args.source).resolve()
    destination = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    validate_output_available(destination, source, force=args.force)
    destination.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    staging = destination.with_name(f".{destination.name}.conversion-{uuid.uuid4().hex}")
    staged_args = copy.copy(args)
    staged_args.output = str(staging)
    staged_args.force = False
    try:
        result = _convert_clone_streams_in_place(staged_args, tools)
        if not conversion_succeeded(result, require_makemkv=getattr(args, "require_makemkv", False)):
            # Keep the complete failed result outside staging before cleanup.
            # Each attempt gets a new file, preserving earlier diagnostic reports.
            report = DEFAULT_REPORT_DIR / "validation-failures" / f"{safe_name(destination.name)}-{uuid.uuid4().hex}.json"
            validate_output_available(report, source, force=False)
            report.parent.mkdir(parents=True, exist_ok=True)
            with report.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
            raise ToolError(f"Replacement failed validation; previous output has been preserved. Full validation report: {report}")
        swap_directory(staging, destination, force=args.force)
        def relocate(value):
            if isinstance(value, str): return value.replace(str(staging), str(destination))
            if isinstance(value, list): return [relocate(item) for item in value]
            if isinstance(value, dict): return {key: relocate(item) for key,item in value.items()}
            return value
        result = relocate(result)
        plan = path_or_none(getattr(args, "progress_plan", None))
        if plan and plan.is_file():
            save_job(plan, relocate(json.loads(plan.read_text(encoding="utf-8"))))
        return result
    finally:
        if staging.exists():
            if staging.parent != destination.parent or not staging.name.startswith("." + destination.name + ".conversion-"):
                raise ToolError("Unsafe staging cleanup")
            shutil.rmtree(staging)


def _convert_clone_streams_in_place(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    from . import conversion_workflow
    return conversion_workflow._convert_clone_streams_in_place(args, tools, services=sys.modules[__name__])


def cmd_tools(args: argparse.Namespace) -> int:
    tools = discover_tools()
    if getattr(args, "json", False):
        print(json.dumps(tools, indent=2))
    else:
        print("BD2HEVC tool check")
        for key in ("ffmpeg", "ffprobe", "tsmuxer", "vlc"):
            print(f"{key}: {tools.get(key) or 'not found'}")
        makemkv = tools.get("makemkvcon64") or tools.get("makemkvcon")
        print(f"makemkvcon: {makemkv or 'not found (optional)'}")
        available_hevc = tools.get("hevc_encoders") or []
        print(f"HEVC encoders: {', '.join(available_hevc) if available_hevc else 'none found'}")
        print(f"hardware encode-ahead: {'available' if any(encoder_is_hardware(e) for e in available_hevc) else 'not available'}")
        try:
            print(f"UDF 2.50 ISO author: {find_udf_tool()}")
        except ToolError:
            print("UDF 2.50 ISO author: not found (required only for ISO output)")
    missing = [k for k in ("ffmpeg", "ffprobe", "tsmuxer") if not tools.get(k)]
    if missing:
        print(f"Missing tools: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not tools.get("hevc_encoders"):
        print("FFmpeg is present, but no supported HEVC encoder was reported.", file=sys.stderr)
        return 3
    if not tools.get("vlc"):
        print("VLC was not found; headless VLC smoke tests will be unavailable.", file=sys.stderr)
    if not (tools.get("makemkvcon64") or tools.get("makemkvcon")):
        print("MakeMKV CLI was not found; MakeMKV title validation will be skipped unless explicitly required.", file=sys.stderr)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui()


def cmd_scan(args: argparse.Namespace) -> int:
    tools = discover_tools()
    roots = find_disc_roots([Path(p) for p in args.paths])
    if not roots:
        raise ToolError("No BDMV backups found")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for root in roots:
        print(f"Scanning {root.name}...", flush=True)
        bitrate_options = bitrate_options_for_args(args)
        report = scan_disc(
            root,
            tools,
            accurate_video_bitrate=args.accurate_video_bitrate,
            depad_video_padding=not getattr(args, "keep_source_padding", False),
            bitrate_options=bitrate_options,
            use_makemkv=use_makemkv_from_args(args),
            verbose=args.verbose,
        )
        remember_original_clip_actions(report.get("clips", []))
        report["main_feature"] = main_feature_selection(
            root,
            known_clip_ids={Path(str(clip.get("file") or "")).stem for clip in report.get("clips", [])},
        )
        report["quality_overrides"] = apply_quality_overrides(
            report.get("clips", []), bitrate_options, args, report.get("main_feature")
        )
        report["copy_clip_overrides"] = apply_clip_copy_overrides(report.get("clips", []), getattr(args, "copy_clips", None))
        report["postprocess"] = apply_deinterlace_plan(report.get("clips", []), args)
        report["summary"] = summarize_disc(report)
        reports.append(report)
        out = report_dir / f"{root.name}.scan.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], indent=2), flush=True)
        print(f"Wrote {out}", flush=True)
    index = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reports": [str((report_dir / f"{Path(r['source']).name}.scan.json").resolve()) for r in reports],
        "summaries": {r["disc"]: r["summary"] for r in reports},
    }
    (report_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    return 0


def planned_clip_quality_text(clip: dict[str, Any]) -> str:
    from . import clip_listing
    return clip_listing.planned_clip_quality_text(clip, services=sys.modules[__name__])


def planned_clip_output_codec(clip: dict[str, Any]) -> str | None:
    from . import clip_listing
    return clip_listing.planned_clip_output_codec(clip, services=sys.modules[__name__])


def field_order_label(field_order: Any) -> str:
    from . import clip_listing
    return clip_listing.field_order_label(field_order, services=sys.modules[__name__])


def clip_list_rows(clips: list[dict[str, Any]], *, sort: str = "duration") -> list[dict[str, Any]]:
    from . import clip_listing
    return clip_listing.clip_list_rows(clips, sort=sort, services=sys.modules[__name__])


def print_clip_list(rows: list[dict[str, Any]]) -> None:
    from . import clip_listing
    return clip_listing.print_clip_list(rows, services=sys.modules[__name__])


def cmd_clips(args: argparse.Namespace) -> int:
    validate_cq_override_args(args)
    tools = discover_tools()
    roots = find_disc_roots([Path(args.source)])
    if not roots:
        raise ToolError(f"No BDMV folder found at {args.source}")
    source = roots[0]
    bitrate_options = bitrate_options_for_args(args)
    report = scan_disc(
        source,
        tools,
        accurate_video_bitrate=args.accurate_video_bitrate,
        depad_video_padding=not getattr(args, "keep_source_padding", False),
        bitrate_options=bitrate_options,
        use_makemkv=use_makemkv_from_args(args),
        verbose=args.verbose,
    )
    remember_original_clip_actions(report.get("clips", []))
    feature_selection = main_feature_selection(
        source,
        known_clip_ids={Path(str(clip.get("file") or "")).stem for clip in report.get("clips", [])},
    )
    quality_overrides = apply_quality_overrides(
        report.get("clips", []), bitrate_options, args, feature_selection
    )
    copy_clip_overrides = apply_clip_copy_overrides(report.get("clips", []), getattr(args, "copy_clips", None))
    postprocess = apply_deinterlace_plan(report.get("clips", []), args)
    rows = clip_list_rows(report.get("clips", []), sort=args.sort)
    payload = {
        "source": str(source),
        "sort": args.sort,
        "bitrate": bitrate_options,
        "main_feature": feature_selection,
        "quality_overrides": quality_overrides,
        "copy_clip_overrides": copy_clip_overrides,
        "postprocess": postprocess,
        "clips": rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"BD2HEVC clips for {source.name}")
        print_clip_list(rows)
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    slot_path = DEFAULT_JOB_DIR / "active-work.lock"
    if args.dry_run or inherited_work_slot(slot_path):
        return _cmd_convert(args)
    slot = FileLock(slot_path, timeout=0)
    if not slot.acquire():
        raise ToolError("Another disc is already converting. Queue this disc or wait for it to finish.")
    try:
        return _cmd_convert(args)
    finally:
        slot.release()


def _cmd_convert(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    if args.mode == "movie-only":
        if getattr(args, "output_format", "folder") == "iso":
            raise ToolError("ISO output is available for full-disc clone-streams mode, not movie-only mode")
        result = convert_movie_only(args, tools)
    else:
        result = convert_clone_streams_with_output_format(args, tools)
    report_path = path_or_none(getattr(args, "report", None))
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print_conversion_summary(result, report_path=report_path, dry_run=args.dry_run)
    return 0 if conversion_succeeded(result, require_makemkv=getattr(args, "require_makemkv", False), dry_run=args.dry_run) else 4


def convert_clone_streams_with_output_format(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if getattr(args, "output_format", "folder") != "iso":
        return convert_clone_streams(args, tools)
    source = Path(args.source).resolve()
    requested = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    final_iso = iso_output_path(requested)
    staging = iso_staging_path(final_iso)
    if not args.dry_run:
        validate_output_available(final_iso, source, force=args.force)
        if final_iso.is_dir():
            raise ToolError(f"Output ISO path is a directory: {final_iso}")
    folder_args = copy.copy(args)
    folder_args.output_format = "folder"
    folder_args.output = str(staging)
    result = convert_clone_streams(folder_args, tools)
    result["output_format"] = "iso"
    result["folder_staging"] = str(staging)
    result["output"] = str(final_iso)
    if args.dry_run:
        return result
    if not conversion_succeeded(result, require_makemkv=getattr(args, "require_makemkv", False)):
        return result
    progress_event("iso-author-start", final_iso.name)
    result["iso_authoring"] = author_bluray_iso(
        staging,
        final_iso,
        tool=getattr(args, "iso_author_tool", None),
        label=source.name,
        force=args.force,
        verbose=args.verbose,
    )
    progress_event("iso-author-done", final_iso.name)
    if not getattr(args, "keep_iso_staging", False):
        shutil.rmtree(staging)
        result["folder_staging_removed"] = True
    return result


def cmd_author_iso(args: argparse.Namespace) -> int:
    report = author_bluray_iso(
        args.source,
        args.output,
        tool=args.iso_author_tool,
        label=args.label,
        force=args.force,
        verbose=args.verbose,
    )
    report_path = path_or_none(args.report)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Blu-ray ISO created: {report['image']}")
        print(f"UDF revision: {report['udf_revision']}  files verified: {report['files']}")
    return 0


def cmd_verify_iso(args: argparse.Namespace) -> int:
    report = verify_bluray_iso(args.image, tool=args.iso_author_tool, verbose=args.verbose, reference=getattr(args, "reference", None), manifest=getattr(args, "manifest", None))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Blu-ray ISO verification passed: {report['image']}")
        print(f"UDF revision: {report['udf_revision']}  files: {report['files']}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    args.output = args.output or None
    args.mode = "clone-streams"
    args.uhd_scale = False
    args.skip_audio = False
    args.skip_subtitles = False
    args.patch_navigation = not args.no_patch_navigation
    args.bdj_compatibility_patches = not args.no_bdj_compatibility_patches
    return cmd_convert(args)


def cmd_validate(args: argparse.Namespace) -> int:
    tools = discover_tools()
    target = Path(args.target).resolve()
    if not target.exists():
        raise ToolError(f"Validation target does not exist: {target}")
    reference_stream_dir: Path | None = None
    if args.reference:
        reference_root = Path(args.reference).resolve()
        reference_roots = find_disc_roots([reference_root])
        if not reference_roots:
            raise ToolError(f"No reference BDMV folder found at {reference_root}")
        reference_stream_dir = reference_roots[0] / "BDMV" / "STREAM"
    roots = find_disc_roots([target])
    require_hevc = "never" if args.source_backup else "over-threshold"
    if roots:
        stream_dir = roots[0] / "BDMV" / "STREAM"
        clips = sorted(stream_dir.glob("*.m2ts"))
        makemkv_validation = validate_disc_titles(
            roots[0],
            tools,
            use_makemkv=use_makemkv_from_args(args),
            require_makemkv=args.require_makemkv,
            verbose=args.verbose,
        )
    else:
        clips = [target]
        makemkv_validation = None
    if not clips or any(not clip.is_file() for clip in clips):
        raise ToolError(f"Validation requires at least one existing media clip: {target}")
    results = []
    for clip in clips:
        reference_clip = reference_stream_dir / clip.name if reference_stream_dir else None
        results.append(
            validate_clip(
                reference_clip,
                clip,
                tools,
                decode_seconds=args.decode_sample,
                require_hevc=require_hevc,
                audio_mode=getattr(args, "audio_mode", DEFAULT_AUDIO_MODE),
            )
        )
    payload: dict[str, Any] = {"source_backup": args.source_backup, "reference": args.reference, "clips": results}
    if makemkv_validation is not None:
        payload["makemkv_validation"] = makemkv_validation
    ok = bool(results) and all(r.get("ok") for r in results) and (makemkv_validation is None or makemkv_validation.get("ok") or not args.require_makemkv)
    report_path = path_or_none(getattr(args, "report", None))
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        bad = [Path(r.get("output", "")).name for r in results if not r.get("ok")]
        print("BD2HEVC validation " + ("passed" if ok else "failed"))
        print(f"Checked clips: {len(results)}")
        if bad:
            print("Failed clips: " + ", ".join(bad[:12]))
        if makemkv_validation is not None:
            if makemkv_validation.get("skipped"):
                print("MakeMKV: skipped")
            else:
                print("MakeMKV: " + ("passed" if makemkv_validation.get("ok") else "failed"))
        if report_path:
            print(f"Full report saved to: {report_path}")
    return 0 if ok else 4


def cmd_playlist_probe(args: argparse.Namespace) -> int:
    tools = discover_tools()
    roots = find_disc_roots([Path(args.target).resolve()])
    if not roots:
        raise ToolError(f"No BDMV folder found at {args.target}")
    reference_root = None
    if args.reference:
        reference_roots = find_disc_roots([Path(args.reference).resolve()])
        if not reference_roots:
            raise ToolError(f"No reference BDMV folder found at {args.reference}")
        reference_root = reference_roots[0]
    report = validate_bluray_playlist(
        roots[0],
        args.playlist,
        tools,
        reference_root=reference_root,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        reference_tolerance=args.reference_tolerance,
        count_frames=args.count_frames,
        min_video_frames=args.min_video_frames,
        decode_seconds=args.decode_seconds,
        fail_on_eof=not args.allow_eof,
    )
    payload = {"playlist_probe": report}
    if args.report:
        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if report.get("ok") else 4


def cmd_patch_disc_metadata(args: argparse.Namespace) -> int:
    roots = find_disc_roots([Path(p) for p in args.paths])
    if not roots:
        raise ToolError("No BDMV backups found")
    reports = []
    for root in roots:
        reports.append(ensure_disc_library_metadata(root, title=args.title, force=args.force))
    print(json.dumps({"patched": reports}, indent=2))
    return 0


def cmd_patch_uhd_profile(args: argparse.Namespace) -> int:
    roots = find_disc_roots([Path(p) for p in args.paths])
    if not roots:
        raise ToolError("No BDMV backups found")
    reports = [ensure_uhd_backup_structure(root, patch_version_headers=patch_version_headers_from_args(args)) for root in roots]
    payload = {"patched": reports}
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print("BD2HEVC UHD profile patch complete")
        for report in reports:
            root = report.get("root")
            created = len(report.get("created_dirs") or [])
            version_patches = sum(1 for item in report.get("version_patches") or [] if item.get("patched"))
            missing = report.get("missing_required_files") or []
            print(f"Output: {root}")
            print(f"  Created folders: {created}")
            target = report.get("version_header_target")
            action = "Patched UHD version headers" if target == "uhd" else "Restored BD-style version headers"
            print(f"  {action}: {version_patches}")
            if missing:
                print("  Missing required files: " + ", ".join(missing))
            cert = report.get("certificate") or {}
            if not cert.get("id_bdmv_exists"):
                print("  Certificate id.bdmv: missing (not generated)")
    return 0


def cmd_patch_navigation(args: argparse.Namespace) -> int:
    tools = discover_tools()
    target = Path(args.target).resolve()
    roots = find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    source_root = None
    if getattr(args, "reference", None):
        source_roots = find_disc_roots([Path(args.reference).resolve()])
        if not source_roots:
            raise ToolError(f"No reference BDMV folder found at {args.reference}")
        source_root = source_roots[0]
    if args.clips:
        clips = args.clips
    else:
        stream_dir = roots[0] / "BDMV" / "STREAM"
        clips = [
            clip["file"]
            for clip in (
                inspect_clip(path, tools, accurate_video_bitrate=False)
                for path in sorted(stream_dir.glob("*.m2ts"))
            )
            if (clip.get("video") or {}).get("codec_name") == "hevc" and (clip.get("duration") or 0) > SECONDS_REENCODE_THRESHOLD
        ]
    report = patch_navigation_for_hevc(
        roots[0],
        clips,
        tools=tools,
        source_root=source_root,
        refresh_cpi=args.refresh_cpi,
        patch_version_headers=patch_version_headers_from_args(args),
        verbose=args.verbose,
    )
    print(json.dumps(report, indent=2))
    return 0


def cmd_remux_replacements(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffprobe", "tsmuxer"):
        require_tool(tools, key)
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    source_roots = find_disc_roots([source])
    output_roots = find_disc_roots([output])
    if not source_roots:
        raise ToolError(f"No source BDMV folder found at {source}")
    if not output_roots:
        raise ToolError(f"No output BDMV folder found at {output}")
    source_root = source_roots[0]
    output_root = output_roots[0]
    source_stream = source_root / "BDMV" / "STREAM"
    output_stream = output_root / "BDMV" / "STREAM"
    if args.clips:
        clip_names = [clip if clip.lower().endswith(".m2ts") else f"{clip}.m2ts" for clip in args.clips]
    else:
        clip_names = []
        for clip in sorted(output_stream.glob("*.m2ts")):
            info = inspect_clip(clip, tools, accurate_video_bitrate=False)
            video = info.get("video") or {}
            if video.get("codec_name") == "hevc" and float(info.get("duration") or 0) > SECONDS_REENCODE_THRESHOLD:
                clip_names.append(clip.name)
    reports = []
    for name in clip_names:
        source_clip = source_stream / name
        output_clip = output_stream / name
        clip_id = Path(name).stem
        output_clpi = output_root / "BDMV" / "CLIPINF" / f"{clip_id}.clpi"
        backup_clpi = output_root / "BDMV" / "BACKUP" / "CLIPINF" / f"{clip_id}.clpi"
        report = remux_replacement_clip(source_clip, output_clip, output_clpi, tools, verbose=args.verbose)
        if output_clpi.exists():
            backup_clpi.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_clpi, backup_clpi)
            report["backup_clpi"] = str(backup_clpi)
        reports.append(report)
    navigation_patch = patch_navigation_for_hevc(output_root, clip_names, tools=tools, source_root=source_root)
    payload = {
        "source": str(source_root),
        "output": str(output_root),
        "clips": clip_names,
        "reports": reports,
        "navigation_patch": navigation_patch,
        "ok": all(item.get("ok") for item in reports),
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 4


def cmd_reencode_replacements(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    source_roots = find_disc_roots([source])
    output_roots = find_disc_roots([output])
    if not source_roots:
        raise ToolError(f"No source BDMV folder found at {source}")
    if not output_roots:
        raise ToolError(f"No output BDMV folder found at {output}")
    source_root = source_roots[0]
    output_root = output_roots[0]
    source_stream = source_root / "BDMV" / "STREAM"
    output_stream = output_root / "BDMV" / "STREAM"
    if args.clips:
        clip_names = [clip if clip.lower().endswith(".m2ts") else f"{clip}.m2ts" for clip in args.clips]
    else:
        raise ToolError("reencode-replacements requires --clips so it cannot accidentally reencode an entire disc")
    reports = []
    for name in clip_names:
        source_clip = source_stream / name
        output_clip = output_stream / name
        clip_id = Path(name).stem
        output_clpi = output_root / "BDMV" / "CLIPINF" / f"{clip_id}.clpi"
        backup_clpi = output_root / "BDMV" / "BACKUP" / "CLIPINF" / f"{clip_id}.clpi"
        report = reencode_replacement_clip(
            source_clip,
            output_clip,
            output_clpi,
            tools,
            hevc_bit_depth=args.hevc_bit_depth,
            encoder=selected_hevc_encoder(args),
            decode_sample=args.decode_sample,
            bitrate_options=bitrate_options_for_args(args),
            verbose=args.verbose,
        )
        if output_clpi.exists():
            backup_clpi.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_clpi, backup_clpi)
            report["backup_clpi"] = str(backup_clpi)
        reports.append(report)
    navigation_patch = patch_navigation_for_hevc(output_root, clip_names, tools=tools, source_root=source_root)
    payload = {
        "source": str(source_root),
        "output": str(output_root),
        "clips": clip_names,
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": selected_hevc_encoder(args),
        "reports": reports,
        "navigation_patch": navigation_patch,
        "ok": all(item.get("ok") for item in reports),
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 4


def cmd_repair_compact_audio(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    target = Path(args.target).resolve()
    roots = find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    output_root = roots[0]
    if args.clips:
        requested_names = normalize_clip_names(args.clips)
        output_stream = output_root / "BDMV" / "STREAM"
        missing = [name for name in requested_names if not (output_stream / name).is_file()]
        if missing:
            raise ToolError(f"--clips referenced unknown clip(s): {', '.join(missing)}")
        clips = [
            inspect_clip(output_stream / name, tools, accurate_video_bitrate=False)
            for name in requested_names
        ]
        candidates = clips
        scan_scope = "requested-clips"
    else:
        scan = scan_disc(
            output_root,
            tools,
            accurate_video_bitrate=False,
            use_makemkv=False,
            verbose=args.verbose,
        )
        clips = scan.get("clips", [])
        candidates = clips
        scan_scope = "full-disc"
    stereo_bitrate = stereo_audio_bitrate_from_args(args)
    mono_bitrate = mono_audio_bitrate_from_args(args)
    selected: list[dict[str, Any]] = []
    for clip in candidates:
        reasons = compact_audio_repair_reasons(
            clip,
            stereo_audio_bitrate=stereo_bitrate,
            mono_audio_bitrate=mono_bitrate,
        )
        if not reasons:
            continue
        selected.append(
            {
                "file": clip.get("file"),
                "path": clip.get("path"),
                "duration": clip.get("duration"),
                "video_codec": (clip.get("video") or {}).get("codec_name"),
                "reasons": reasons,
                "before_bytes": Path(str(clip.get("path"))).stat().st_size,
            }
        )
    payload: dict[str, Any] = {
        "mode": "repair-compact-audio",
        "output": str(output_root),
        "audio": {
            "mode": "compact-stereo",
            "stereo_bitrate": stereo_bitrate,
            "mono_bitrate": mono_bitrate,
        },
        "scanned_clips": len(clips),
        "scan_scope": scan_scope,
        "selected_count": len(selected),
        "selected": selected,
        "dry_run": bool(args.dry_run),
    }
    report_path = path_or_none(getattr(args, "report", None))
    if args.dry_run:
        payload["ok"] = True
        if report_path:
            save_job(report_path, payload)
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print("BD2HEVC compact-audio repair plan")
            print(f"Output: {output_root}")
            print(f"Clips to remux: {len(selected)}")
            if selected:
                print("Clips: " + ", ".join(str(item.get("file")) for item in selected[:12]))
            print("Video and subtitles will be stream-copied; no video encoder is used.")
        return 0

    clip_lookup = clip_lookup_by_name(clips)
    reports: list[dict[str, Any]] = []
    for index, item in enumerate(selected, start=1):
        name = normalize_clip_name(item.get("file"))
        clip = clip_lookup[name]
        print(f"Compact-audio remux {index}/{len(selected)}: {name}", flush=True)
        report = remux_compact_audio_copy_context(clone_clip_context(output_root, output_root, clip), tools, args)
        output_path = output_root / "BDMV" / "STREAM" / name
        final_info = inspect_clip(output_path, tools, accurate_video_bitrate=False)
        navigation_patch = patch_navigation_for_hevc(
            output_root,
            [name] if (final_info.get("video") or {}).get("codec_name") == "hevc" else [],
            tools=tools,
            compact_audio_clip_files=[name],
            patch_version_headers=patch_version_headers_from_args(args),
        )
        final_audio = compact_audio_source_streams(final_info)
        audio_ok = bool(final_audio) and all(
            compact_audio_stream_matches_target(
                audio,
                stereo_audio_bitrate=stereo_bitrate,
                mono_audio_bitrate=mono_bitrate,
            )
            for audio in final_audio
        )
        report.update(
            {
                "file": name,
                "before_bytes": item.get("before_bytes"),
                "after_bytes": output_path.stat().st_size,
                "audio_ok": audio_ok,
                "navigation_patch": navigation_patch,
                "audio": [
                    {
                        "codec": audio.get("codec_name"),
                        "channels": audio.get("channels"),
                        "bitrate": audio.get("bit_rate"),
                    }
                    for audio in final_audio
                ],
            }
        )
        report["ok"] = bool(report.get("ok")) and audio_ok
        if not report["ok"]:
            raise ToolError(f"Compact-audio repair validation failed for {name}")
        reports.append(report)
    uhd_structure = ensure_uhd_backup_structure(
        output_root,
        patch_version_headers=patch_version_headers_from_args(args),
    )
    makemkv_validation = validate_disc_titles(
        output_root,
        tools,
        use_makemkv=use_makemkv_from_args(args),
        require_makemkv=args.require_makemkv,
        verbose=args.verbose,
    )
    payload.update(
        {
            "dry_run": False,
            "reports": reports,
            "repaired": [report.get("file") for report in reports],
            "bytes_saved": sum(
                max(0, int(report.get("before_bytes") or 0) - int(report.get("after_bytes") or 0))
                for report in reports
            ),
            "navigation_patches": [report.get("navigation_patch") for report in reports],
            "uhd_structure": uhd_structure,
            "makemkv_validation": makemkv_validation,
        }
    )
    payload["ok"] = all(report.get("ok") for report in reports) and (
        bool(makemkv_validation.get("ok")) or not args.require_makemkv
    )
    if report_path:
        save_job(report_path, payload)
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print("BD2HEVC compact-audio repair " + ("complete" if payload["ok"] else "failed"))
        print(f"Output: {output_root}")
        print(f"Repaired clips: {len(reports)}")
        print(f"Space saved: {payload['bytes_saved'] / (1024 ** 3):.2f} GiB")
        if makemkv_validation.get("skipped"):
            print("MakeMKV: skipped")
        else:
            print("MakeMKV: " + ("passed" if makemkv_validation.get("ok") else "failed"))
        if report_path:
            print(f"Full report saved to: {report_path}")
    return 0 if payload["ok"] else 4


def cmd_repair_output(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    source_roots = find_disc_roots([source])
    output_roots = find_disc_roots([output])
    if not source_roots:
        raise ToolError(f"No source BDMV folder found at {source}")
    if not output_roots:
        raise ToolError(f"No output BDMV folder found at {output}")
    source_root = source_roots[0]
    output_root = output_roots[0]
    selected = select_output_repair_clips(
        source_root,
        output_root,
        tools,
        hevc_bit_depth=args.hevc_bit_depth,
        requested_clips=args.clips,
    )
    clip_names = [item["clip"] for item in selected]
    if args.dry_run:
        payload = {
            "source": str(source_root),
            "output": str(output_root),
            "hevc_bit_depth": args.hevc_bit_depth,
            "selected_count": len(selected),
            "selected": selected,
            "ok": True,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print("BD2HEVC repair plan")
            print(f"Output: {output_root}")
            print(f"Clips to repair: {len(selected)}")
            if selected:
                print("Clips: " + ", ".join(item["clip"] for item in selected[:12]))
        return 0

    source_stream = source_root / "BDMV" / "STREAM"
    output_stream = output_root / "BDMV" / "STREAM"
    reports = []
    for name in clip_names:
        source_clip = source_stream / name
        output_clip = output_stream / name
        clip_id = Path(name).stem
        output_clpi = output_root / "BDMV" / "CLIPINF" / f"{clip_id}.clpi"
        backup_clpi = output_root / "BDMV" / "BACKUP" / "CLIPINF" / f"{clip_id}.clpi"
        report = reencode_replacement_clip(
            source_clip,
            output_clip,
            output_clpi,
            tools,
            hevc_bit_depth=args.hevc_bit_depth,
            encoder=selected_hevc_encoder(args),
            decode_sample=args.decode_sample,
            bitrate_options=bitrate_options_for_args(args),
            verbose=args.verbose,
        )
        if output_clpi.exists():
            backup_clpi.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_clpi, backup_clpi)
            report["backup_clpi"] = str(backup_clpi)
        reports.append(report)
    navigation_patch = patch_navigation_for_hevc(output_root, clip_names, tools=tools, source_root=source_root) if clip_names else None
    uhd_structure = ensure_uhd_backup_structure(output_root)
    payload = {
        "source": str(source_root),
        "output": str(output_root),
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": selected_hevc_encoder(args),
        "selected_count": len(selected),
        "selected": selected,
        "reports": reports,
        "navigation_patch": navigation_patch,
        "uhd_structure": uhd_structure,
        "ok": all(item.get("ok") for item in reports),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        repaired = [item.get("clip") for item in reports if item.get("ok")]
        failed = [item.get("clip") for item in reports if not item.get("ok")]
        print("BD2HEVC repair " + ("complete" if payload["ok"] else "failed"))
        print(f"Output: {output_root}")
        print(f"Repaired clips: {len(repaired)}")
        if repaired:
            print("Clips: " + ", ".join(repaired[:12]))
        if failed:
            print("Failed: " + ", ".join(failed[:12]))
    return 0 if payload["ok"] else 4


def cmd_patch_vlc_compat(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    fixes = compatibility_fix_names_from_args(args)
    custom_patch_files = custom_compatibility_patch_files_from_args(args)
    report = patch_known_bdj_compatibility(target, fixes=fixes, custom_patch_files=custom_patch_files)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        print("BD2HEVC VLC compatibility patch " + ("complete" if report.get("patched") else "made no changes"))
        print(f"Target: {report.get('target')}")
        if fixes:
            print("Built-in fixes: " + ", ".join(fixes))
        if custom_patch_files:
            print("Custom patch files: " + ", ".join(str(path) for path in custom_patch_files))
        print(f"Patches considered: {len(report.get('patches') or [])}")
    return 0 if report.get("patched") or report.get("patches") is not None else 4


def vlc_bluray_uri(root: Path) -> str:
    return "bluray:///" + root.as_posix()


def clean_vlc_bluray_command(vlc: str, root: Path, *, region: str | None = None) -> list[str]:
    cmd = [
        vlc,
        "--no-one-instance",
        "--no-playlist-enqueue",
        "--qt-continue=0",
        "--no-video-title-show",
        "--bluray-menu",
    ]
    if region:
        cmd.append(f"--bluray-region={region.upper()}")
    cmd.append(vlc_bluray_uri(root))
    return cmd


def cmd_play(args: argparse.Namespace) -> int:
    tools = discover_tools()
    vlc = require_tool(tools, "vlc")
    target = Path(args.target).resolve()
    roots = [target] if target.is_file() and target.suffix.lower() == ".iso" else find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    root = roots[0]
    cmd = clean_vlc_bluray_command(vlc, root, region=args.region)
    env = refreshed_env()
    if getattr(args, "no_bdj_persistent_storage", False):
        env["LIBBLURAY_PERSISTENT_STORAGE"] = "no"
    if getattr(args, "dry_run", False):
        payload = {
            "target": str(root),
            "command": format_cmd(cmd),
            "no_bdj_persistent_storage": bool(getattr(args, "no_bdj_persistent_storage", False)),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print(payload["command"])
        return 0
    subprocess.Popen(cmd, env=env)
    if getattr(args, "json", False):
        print(json.dumps({"opened": str(root), "command": format_cmd(cmd)}, indent=2))
    else:
        print(f"Opened in VLC: {root}")
    return 0


def cmd_vlc_smoke(args: argparse.Namespace) -> int:
    tools = discover_tools()
    vlc = require_tool(tools, "vlc")
    target = Path(args.target).resolve()
    roots = find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    root = roots[0]
    log_path = Path(args.log).resolve() if args.log else DEFAULT_REPORT_DIR / f"{root.name}.vlc_headless_smoke.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    cmd = [
        vlc,
        "-I",
        "dummy",
        "--dummy-quiet",
        "--no-one-instance",
        "--no-playlist-enqueue",
        "--no-qt-privacy-ask",
    ]
    if args.video_plane:
        cmd.extend(["--vout", "dummy", "--aout", "dummy"])
    else:
        cmd.extend(["--no-video", "--aout", "dummy"])
    if args.d3d11:
        cmd.append("--avcodec-hw=d3d11va")
    if not args.allow_resume:
        cmd.append("--qt-continue=0")
    cmd.extend(
        [
            "--no-video-title-show",
            "--play-and-exit",
            f"--run-time={args.seconds}",
            "--file-logging",
            f"--logfile={log_path}",
            "--verbose=2",
            "--bluray-menu",
        ]
    )
    if args.region:
        cmd.append(f"--bluray-region={args.region.upper()}")
    cmd.append(vlc_bluray_uri(root))
    bdj_storage_env: dict[str, str] = {}
    if getattr(args, "isolated_bdj_storage", False):
        label = f"{root.parent.name or root.name}-{int(time.time())}"
        bdj_storage_env = isolated_bdj_storage_env(DEFAULT_REPORT_DIR / "libbluray-smoke-storage", label)
    if getattr(args, "no_bdj_persistent_storage", False):
        bdj_storage_env["LIBBLURAY_PERSISTENT_STORAGE"] = "no"
    proc = run_cmd(
        cmd,
        check=False,
        capture=True,
        timeout_seconds=args.seconds + 30,
        verbose=args.verbose,
        env_overrides=bdj_storage_env,
    )
    text = read_text_flexible(log_path) if log_path.exists() else ""
    bad_patterns = [
        r"\bBD-J\b.*\berror\b",
        r"\bbdj\b.*\berror\b",
        r"\blibdvbpsi error\b.*\bTS discontinuity\b",
        r"Can't read TS packet at 768\b",
        r"\bException\b",
        r"\bSecurityException\b",
        r"\bsignature\b.*\bfailed\b",
        r"\bjar\b.*\bfailed\b",
    ]
    if not args.video_plane:
        bad_patterns.extend([r"\bTimestamp conversion failed\b", r"\bCould not convert timestamp\b"])
    if args.video_plane and args.d3d11:
        bad_patterns.extend(
            [
                r"\bUnsupported bitdepth\b",
                r"\bnot enough decoding slices\b",
                r"\bhardware acceleration picture allocation failed\b",
                r"\bavcodec_send_packet critical error\b",
                r"\bpicture is too late\b",
                r"\bexisting hardware acceleration cannot be reused\b",
                r"\bno matching alpha blending routine\b.*\bDX10\b",
                r"\bblending\b.*\bDX10 failed\b",
            ]
        )
    errors = sorted({m.group(0) for pat in bad_patterns for m in re.finditer(pat, text, re.IGNORECASE)})
    opened = "using access_demux module \"libbluray\"" in text or "successfully opened" in text
    bdj_titles = re.search(r"BD-J Titles:\s*(\d+)", text)
    counters = {
        "timestamp_conversion_failed": len(re.findall(r"Timestamp conversion failed", text)),
        "could_not_convert_timestamp": len(re.findall(r"Could not convert timestamp", text)),
        "rawvideo_invalid_frame_rate": len(re.findall(r"rawvideo warning: invalid frame rate", text)),
        "libdvbpsi_ts_discontinuity_errors": len(re.findall(r"libdvbpsi error.*TS discontinuity", text)),
        "cant_read_ts_packet": len(re.findall(r"Can't read TS packet", text)),
        "adding_es": len(re.findall(r"Adding ES", text)),
        "reusing_es": len(re.findall(r"Reusing ES", text)),
        "bdj_event_32": len(re.findall(r"event:\s*32", text)),
        "bdj_event_25": len(re.findall(r"event:\s*25", text)),
        "bdj_start_background": len(re.findall(r"Start background", text)),
        "bdj_stop_background": len(re.findall(r"Stop background", text)),
        "buffer_deadlock": len(re.findall(r"buffer deadlock", text)),
        "d3d11_unsupported_bitdepth": len(re.findall(r"Unsupported bitdepth", text)),
        "d3d11_not_enough_slices": len(re.findall(r"not enough decoding slices", text)),
        "d3d11_using_p010": len(re.findall(r"Using output format P010", text)),
        "d3d11_using_nv12": len(re.findall(r"Using output format NV12", text)),
        "picture_too_late": len(re.findall(r"picture is too late", text)),
        "d3d11_picture_allocation_failed": len(re.findall(r"hardware acceleration picture allocation failed", text)),
        "avcodec_send_packet_critical": len(re.findall(r"avcodec_send_packet critical error", text)),
        "d3d11_hw_reuse_failed": len(re.findall(r"existing hardware acceleration cannot be reused", text)),
        "d3d11_dx10_blending_failed": len(re.findall(r"blending .*DX10 failed|no matching alpha blending routine.*DX10", text)),
    }
    process_ok = proc.returncode in (0, 1) or (proc.returncode == 124 and opened and not errors)
    video_streams_seen = counters["adding_es"] > 0 or counters["reusing_es"] > 0
    bdj_startup_diagnosis = None
    if args.video_plane and opened and bdj_titles and not video_streams_seen and not errors:
        if counters["bdj_event_32"] == 0 and counters["bdj_start_background"] == 0:
            bdj_startup_diagnosis = (
                "libbluray opened the disc and enumerated BD-J titles, but BD-J playback events never started. "
                "This usually points to a VLC/libbluray/JVM startup-state hang rather than invalid converted streams."
            )
        else:
            bdj_startup_diagnosis = (
                "BD-J background playback started, but VLC/libbluray never handed off to a playlist stream. "
                "Compare source and converted logs, and try --isolated-bdj-storage to rule out stale BD-J cache state."
            )
    result = {
        "target": str(root),
        "mode": "headless-dummy-video" if args.video_plane else "headless-no-video",
        "note": "This avoids visible windows. Dummy-video mode exercises VLC's video/subpicture path, but it still cannot prove visual menu overlay state. VLC may ignore --run-time on BD-J menus, so timeout is acceptable after a clean open.",
        "command": format_cmd(cmd),
        "isolated_bdj_storage": bool(getattr(args, "isolated_bdj_storage", False)),
        "bdj_storage_env": bdj_storage_env,
        "bdj_startup_diagnosis": bdj_startup_diagnosis,
        "returncode": proc.returncode,
        "log": str(log_path),
        "counters": counters,
        "checks": [
            {"name": "vlc_completed_or_clean_timeout", "ok": process_ok, "returncode": proc.returncode},
            {"name": "log_written", "ok": log_path.exists()},
            {"name": "libbluray_opened", "ok": opened},
            {"name": "bdj_titles_seen", "ok": bool(bdj_titles), "value": int(bdj_titles.group(1)) if bdj_titles else None},
            {"name": "no_vlc_startup_errors", "ok": not errors, "matches": errors[:10]},
            {
                "name": "video_plane_streams_seen",
                "ok": (not args.video_plane) or video_streams_seen,
                "adding_es": counters["adding_es"],
                "reusing_es": counters["reusing_es"],
                "diagnosis": bdj_startup_diagnosis,
            },
            {
                "name": "no_visible_video_requested",
                "ok": True,
                "flags": ["--vout dummy", "--aout dummy", "-I dummy"] if args.video_plane else ["--no-video", "--aout dummy", "-I dummy"],
            },
        ],
    }
    result["ok"] = all(check.get("ok") for check in result["checks"])
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 4


def cmd_record_libbluray(args: argparse.Namespace) -> int:
    tools = discover_tools()
    vlc = require_tool(tools, "vlc")
    result = create_libbluray_recording(
        target=Path(args.target),
        source=Path(args.source) if args.source else None,
        vlc=vlc,
        label=args.label,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        region=args.region,
        duration=args.duration,
        verbose_level=args.verbose_level,
        isolated_bdj_storage=args.isolated_bdj_storage,
        libbluray_debug_mask=args.libbluray_debug_mask,
        dry_run=args.dry_run,
        keep_folder=args.no_zip,
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    elif result.get("dry_run"):
        print("BD2HEVC VLC/libbluray recorder dry run.")
        print(f"Target: {result['target']}")
        if result.get("source"):
            print(f"Source: {result['source']}")
        print(f"Would save to: {result['bundle']}")
        print(f"VLC command: {result['command']}")
        if result.get("libbluray_debug_mask"):
            print(f"Direct libbluray debug mask: {result['libbluray_debug_mask']}")
    else:
        print("BD2HEVC VLC/libbluray recording complete.")
        print(f"Saved to: {result['bundle']}")
        print("Attach this bundle to a GitHub issue with the exact navigation steps.")
    return 0 if result.get("ok") else 4


def enqueue_conversion_job(args: argparse.Namespace, *, announce: bool = True) -> dict[str, Any]:
    with FileLock(DEFAULT_JOB_DIR / "queue-admission.lock"):
        return _enqueue_conversion_job(args, announce=announce)


def _enqueue_conversion_job(args: argparse.Namespace, *, announce: bool = True) -> dict[str, Any]:
    from .queueing import job_reserves_output
    validate_cq_override_args(args)
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_working_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    source = Path(args.source).resolve()
    roots = find_disc_roots([source])
    if not roots:
        raise ToolError(f"No BDMV folder found at {source}")
    source = roots[0]
    output = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    if getattr(args, "output_format", "folder") == "iso":
        output = iso_output_path(output)
    for existing_path in known_job_files():
        existing = try_load_job(existing_path)
        if existing and job_reserves_output(existing) and Path(str(existing.get("output") or "")).resolve() == output:
            raise ToolError(f"Output is already reserved by job {existing.get('id')}: {output}")
    output_existed_at_queue = output.exists()
    validate_output_available(output, source, force=args.force)
    if getattr(args, "output_format", "folder") == "iso" and output.is_dir():
        raise ToolError(f"Output ISO path is a directory: {output}")
    output_created_by_job = not output_existed_at_queue

    job_id = safe_name(args.name or f"{time.strftime('%Y%m%d-%H%M%S')}-{source.name}")
    paths = job_paths(job_id)
    if paths["job"].exists() and job_reserves_output(try_load_job(paths["job"]) or {}):
        raise ToolError(f"An active job already owns this identifier: {job_id}")
    if paths["job"].exists() and not getattr(args, "force_job", False):
        raise ToolError(f"Job already exists: {job_id}. Use a different --name.")
    command = auto_command_for_job(args, output, paths["report"], paths["plan"])
    queue_order = time.time()
    job = {
        "id": job_id,
        "status": "queued",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queue_order": queue_order,
        "source": str(source),
        "output": str(output),
        "output_existed_at_queue": output_existed_at_queue,
        "output_created_by_job": output_created_by_job,
        "output_format": getattr(args, "output_format", "folder"),
        "staging_output": str(iso_staging_path(output)) if getattr(args, "output_format", "folder") == "iso" else None,
        "plan": str(paths["plan"]),
        "log": str(paths["log"]),
        "report": str(paths["report"]),
        "exitcode": str(paths["exitcode"]),
        "job_file": str(paths["job"]),
        "command": command,
        "reencode_clip_count": None,
    }
    job["queued_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_job(paths["job"], job, allow_cancel_reset=bool(getattr(args, "force_job", False)))
    try:
        pid = start_background_process(paths["job"])
    except BaseException:
        job["status"] = "failed"
        job["error"] = "Worker could not be started"
        save_job(paths["job"], job)
        raise
    # The worker owns its PID/state. Never rewrite the pre-spawn snapshot.
    job = try_load_job(paths["job"]) or job
    job.setdefault("pid", pid)
    if announce:
        print("BD2HEVC background conversion queued")
        print(f"Job: {job_id}")
        print(f"Output: {output}")
        print(f"Log: {paths['log']}")
        print(f"Full report: {paths['report']}")
        print(f"Check progress: python bd2hevc.py status {job_id}")
        print(f"Watch progress: python bd2hevc.py status {job_id} --watch")
    return job


def cmd_start(args: argparse.Namespace) -> int:
    enqueue_conversion_job(args, announce=True)
    return 0


def queue_output_for(source: Path, args: argparse.Namespace) -> Path:
    if getattr(args, "output_dir", None):
        output = generated_output_for(
            source,
            Path(args.output_dir),
            add_tags=bool(getattr(args, "add_output_tags", True)),
        )
    else:
        output = default_output_for(source, "clone-streams")
    return iso_output_path(output) if getattr(args, "output_format", "folder") == "iso" else output


def cmd_queue(args: argparse.Namespace) -> int:
    roots = find_disc_roots([Path(path) for path in args.sources])
    if not roots:
        raise ToolError("No BDMV backups found to queue")
    print(f"Queueing {len(roots)} conversion job(s)...")
    jobs: list[dict[str, Any]] = []
    for index, source in enumerate(roots, start=1):
        job_args = copy.copy(args)
        job_args.source = str(source)
        job_args.output = str(queue_output_for(source, args))
        base_name = args.name_prefix or time.strftime("%Y%m%d-%H%M%S")
        job_args.name = f"{base_name}-{index:02d}-{source.name}"
        job = enqueue_conversion_job(job_args, announce=False)
        jobs.append(job)
        print(f"{index}. {job['id']}")
        print(f"   output: {job['output']}")
    print("Queued jobs run one at a time.")
    print("Check queue: python bd2hevc.py jobs")
    if jobs:
        print(f"Watch first job: python bd2hevc.py status {jobs[0]['id']} --watch")
    return 0


def cmd_preset_save_validated(args: argparse.Namespace) -> int:
    validate_cq_override_args(args)
    return cmd_preset_save(args)


def add_bitrate_args(parser: argparse.ArgumentParser, *, include_named_preset: bool = True, include_file_preset: bool = True) -> None:
    if include_named_preset:
        parser.add_argument("--preset", default=None, help="Load a saved named preset. Use 'python bd2hevc.py preset list' to see available presets.")
    parser.add_argument("--quality", default=None, help="General video handling for reencode-eligible clips. Accepts a bitrate preset, cq:N, source-ratio:N, legacy presets such as anime-cq18/episode-compact, or copy/no-reencode. Overrides --bitrate-mode when set.")
    parser.add_argument("--bitrate-mode", choices=BITRATE_MODES, default="balanced", help="HEVC bitrate preset. balanced is the tested default; smaller saves more space; transparent spends more bitrate; source-ratio uses a fixed multiplier; compact-cq uses configurable CQ for reencoded clips. episode-compact and anime-cq18 are accepted as legacy aliases.")
    if include_file_preset:
        parser.add_argument("--bitrate-preset-file", "--preset-file", dest="bitrate_preset_file", default=None, help="Load bitrate settings from a JSON preset file. Non-default CLI options override preset fields.")
    parser.add_argument("--hevc-bitrate-factor", type=float, default=None, help="Override bitrate mode with a fixed HEVC/source video bitrate multiplier, e.g. 0.62.")
    parser.add_argument("--codec-source-ratio", action="append", default=None, metavar="CODEC=FACTOR", help="Override the HEVC/source multiplier for one source codec, e.g. h264=0.55, mpeg2video=0.30, or vc1=0.45. Can be repeated and overrides the general source ratio for matching clips.")
    parser.add_argument("--min-video-bitrate", type=parse_bitrate_arg, default=2_000_000, help="Minimum target video bitrate. Accepts values like 2000k or 2M.")
    parser.add_argument("--max-video-bitrate", type=parse_bitrate_arg, default=80_000_000, help="Maximum target video bitrate. Accepts values like 80M.")
    parser.add_argument("--maxrate-multiplier", type=float, default=1.55, help="VBV maxrate multiplier relative to target bitrate.")
    parser.add_argument("--bufsize-multiplier", type=float, default=2.0, help="VBV buffer multiplier relative to maxrate.")
    parser.add_argument("--keep-source-padding", action="store_true", help="Keep coded filler/stuffing bytes in source bitrate planning. By default, accurate planning subtracts safe AVC/HEVC/VC-1 padding for library outputs.")
    parser.add_argument("--compact-cq-value", "--anime-cq-value", dest="compact_cq_value", type=int, default=ANIME_CQ_VALUE, help="CQ value for reencoded clips when --bitrate-mode compact-cq is used. Lower is larger/higher quality; default 18.")
    parser.add_argument("--compact-cq-min-duration", "--episode-compact-min-duration", "--anime-cq-min-duration", dest="anime_cq_min_duration", type=parse_duration_arg, default=DEFAULT_ANIME_CQ_MIN_DURATION, help="Minimum clip duration for --bitrate-mode compact-cq to use CQ. Defaults to the 10-second reencode threshold. Raise it if only episode/movie-length clips should use CQ. Accepts values like 15m or 00:15:00. Shorter reencoded clips use smaller. --episode-compact-min-duration and --anime-cq-min-duration are accepted as legacy aliases.")
    parser.add_argument("--main-title-quality", metavar="QUALITY", default=None, help="Quality for every physical clip used by the detected main feature playlist and closely related seamless-branched cuts. Falls back to the longest reencode-eligible clip when playlist topology is unavailable. Accepts a bitrate preset, cq:N, source-ratio:N, legacy presets, or copy/no-reencode. Mutually exclusive with top-N overrides.")
    parser.add_argument("--main-title-bitrate-mode", metavar="MODE", default=None, help="Legacy spelling for --main-title-quality MODE.")
    parser.add_argument("--main-title-cq", type=int, default=None, help="Use compact-cq at this CQ value for all detected main-feature clips, including alternate seamless-branched cuts. Lower is larger/higher quality; useful for CQ20 extras with a CQ18 movie.")
    parser.add_argument("--top-n-quality", nargs=2, metavar=("COUNT", "QUALITY"), default=None, help="Quality for the COUNT longest reencode-eligible clips. QUALITY accepts a bitrate preset, cq:N, source-ratio:N, legacy presets, or copy/no-reencode.")
    parser.add_argument("--top-n-bitrate-mode", nargs=2, metavar=("COUNT", "MODE"), default=None, help="Legacy spelling for --top-n-quality COUNT MODE.")
    parser.add_argument("--top-n-cq", nargs=2, type=int, metavar=("COUNT", "CQ"), default=None, help="Use compact-cq at this CQ value for the COUNT longest reencoded clips. Mutually exclusive with main-title overrides; useful for episode discs, e.g. --top-n-cq 3 18.")
    parser.add_argument("--clip-quality", nargs=2, action="append", metavar=("CLIP", "QUALITY"), default=None, help="Quality for one named M2TS clip. QUALITY accepts a bitrate preset, cq:N, source-ratio:N, legacy presets, or copy/no-reencode. Can be repeated.")
    parser.add_argument("--clip-bitrate-mode", nargs=2, action="append", metavar=("CLIP", "MODE"), default=None, help="Legacy spelling for --clip-quality CLIP MODE. Can be repeated.")
    parser.add_argument("--clip-cq", nargs=2, action="append", metavar=("CLIP", "CQ"), default=None, help="Legacy spelling for --clip-quality CLIP cq:CQ. Can be repeated.")
    parser.add_argument("--copy-clips", "--exclude-clips", dest="copy_clips", nargs="+", action="append", default=None, metavar="CLIP", help="Copy named M2TS clips untouched instead of reencoding them. Accepts 00012 or 00012.m2ts. Can be repeated.")


def add_encoder_args(parser: argparse.ArgumentParser, *, include_encode_ahead: bool = False) -> None:
    parser.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc", help="HEVC encoder to use. Default is hevc_nvenc. Use --encoder libx265, hevc_qsv, or hevc_amf if NVENC is unavailable and your FFmpeg build supports that encoder. Hardware encoders can overlap next-clip encoding with later audio/muxing stages; libx265 stays serial.")
    if include_encode_ahead:
        parser.add_argument("--no-encode-ahead", action="store_true", help="Disable hardware encode-ahead pipelining and run encode/mux serially.")
        parser.add_argument("--encode-ahead-depth", type=int, default=3, help="Maximum completed video/audio outputs per lane allowed to wait for muxing. Hardware encoders only; default 3.")


class DeinterlaceModeAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.deinterlace_explicit = True


def add_postprocess_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deinterlace", choices=DEINTERLACE_MODES, default=DEFAULT_DEINTERLACE_MODE, action=DeinterlaceModeAction, help="Video post-processing for reencoded clips. Default auto deinterlaces clips flagged interlaced by source metadata; off disables deinterlacing; force deinterlaces every reencoded clip.")
    parser.add_argument("--deinterlace-filter", choices=DEINTERLACE_FILTERS, default="bwdif", help="FFmpeg deinterlace filter to use when deinterlacing. bwdif is higher quality; yadif is the compatibility fallback.")
    parser.add_argument("--deinterlace-clips", nargs="+", action="append", default=None, metavar="CLIP", help="Force deinterlacing for named M2TS clips, even when --deinterlace is off or metadata says progressive. Accepts 00043 or 00043.m2ts. Can be repeated.")
    parser.add_argument("--no-deinterlace-clips", nargs="+", action="append", default=None, metavar="CLIP", help="Do not deinterlace named M2TS clips, even when --deinterlace auto/force would select them. Can be repeated.")


def add_audio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio-mode", choices=AUDIO_MODES, default=DEFAULT_AUDIO_MODE, help="Audio handling for full-disc conversion. passthrough keeps source audio; compact-stereo converts every playable audio track to AC-3 stereo, or mono when the source stream is mono, including clips whose video is copied.")
    parser.add_argument("--stereo-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_STEREO_AUDIO_BITRATE, help="Bitrate for compact-stereo two-channel AC-3 audio. Default 256k.")
    parser.add_argument("--mono-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_MONO_AUDIO_BITRATE, help="Bitrate for compact-stereo mono AC-3 audio. Default 128k.")


def add_uhd_output_args(parser: argparse.ArgumentParser) -> None:
    sizes = ", ".join(DISC_SIZE_BYTES)
    parser.add_argument("--uhd-profile", choices=["library", "disc", "auto", "off"], default="library", help="Output profile. library is the normal digital-library mode: UHD-like folders with BD-style navigation headers for VLC compatibility. disc adds physical-disc guardrails, patches navigation headers toward UHD, and requires --target-disc-size with VBR quality. auto/off are legacy aliases for library.")
    parser.add_argument("--target-disc-size", default=None, metavar="SIZE", help=f"Scale VBR video targets to fit a physical-disc budget. Accepts {sizes}, or a size such as 23.5GB. Requires VBR targets, not CQ.")
    parser.add_argument("--target-disc-margin", type=float, default=0.98, help="Safety margin for --target-disc-size. Default 0.98 leaves room for filesystem/authoring overhead.")


def add_iso_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-format", choices=["folder", "iso"], default="folder", help="Write the converted full-disc backup as a normal folder (default) or a verified UDF 2.50 Blu-ray ISO.")
    parser.add_argument("--iso-author-tool", default=None, help="Optional path to a compatible Hadris UDF author. The bundled tool is used by default.")
    parser.add_argument("--keep-iso-staging", action="store_true", help="Keep the converted folder after successfully authoring an ISO. By default it is removed after verification.")


def add_makemkv_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--makemkv", action="store_true", help="Use MakeMKV title scanning/validation for folder backups. Disabled by default to avoid probing physical optical drives.")
    parser.add_argument("--no-makemkv", action="store_true", help="Skip MakeMKV title scanning/validation. Conversion still uses FFprobe/FFmpeg/tsMuxer.")
    parser.add_argument("--require-makemkv", action="store_true", help="Fail if MakeMKV validation is unavailable or fails.")


def add_vlc_compatibility_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vlc-compat", choices=["auto", "off"], default=DEFAULT_VLC_COMPATIBILITY_MODE, help="Apply optional VLC/libbluray compatibility fixes. Use off for the closest possible copy of the source BD-J.")
    parser.add_argument("--vlc-fix", action="append", choices=sorted(KNOWN_VLC_COMPATIBILITY_FIXES), help="Apply a specific built-in VLC compatibility fix. Can be repeated. Overrides the built-in auto fix set.")
    parser.add_argument("--compat-patch-file", action="append", help="JSON file with custom JAR/class compatibility patches.")


def command_parser(sub: argparse._SubParsersAction, name: str, *, help: str, description: str, examples: str) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help,
        description=description,
        epilog="Examples:\n" + examples.strip("\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    from . import cli_parser
    return cli_parser.build_parser(services=sys.modules[__name__])


def add_convert_args(parser: argparse.ArgumentParser, *, source_optional: bool = False, output_optional: bool = False) -> None:
    parser.add_argument("source", nargs="?" if source_optional else None, help="Source BD backup folder.")
    parser.add_argument("output", nargs="?", help="Output folder. Defaults to <source>_FULL_DISC_HEVC in clone-streams mode or <source>_UHDBD_MOVIE_ONLY_HEVC in movie-only mode.")
    parser.add_argument("--mode", choices=["movie-only", "clone-streams"], default="movie-only")
    parser.add_argument("--title", type=int, default=None, help="MakeMKV title id. Defaults to the longest title.")
    parser.add_argument("--extract-with-makemkv", action="store_true", help="Force MakeMKV MKV extraction before transcoding.")
    parser.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    parser.add_argument("--force-encode", action="store_true", help="Encode even when the selected video would normally be copied.")
    parser.add_argument("--uhd-scale", action="store_true", help="Upscale encoded video to 3840x2160 before UHD-BD authoring.")
    parser.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth. 8 preserves 8-bit BD sources and is VLC-friendly; use 10 for explicit Main10 output.")
    add_encoder_args(parser, include_encode_ahead=True)
    add_bitrate_args(parser)
    add_postprocess_args(parser)
    add_iso_output_args(parser)
    parser.add_argument("--skip-audio", action="store_true", help="Diagnostic only: mux video without audio tracks.")
    parser.add_argument("--skip-subtitles", action="store_true", help="Mux audio only with the encoded video; omit PGS subtitle tracks.")
    parser.add_argument("--patch-navigation", action=argparse.BooleanOptionalAction, default=True, help="In clone-streams mode, update CLPI/MPLS primary video descriptors from AVC to HEVC for reencoded clips.")
    parser.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(parser)
    parser.add_argument("--sample-seconds", type=float, default=None, help="Encode only N seconds for a smoke test.")
    parser.add_argument("--sample-start", type=float, default=0.0, help="Start offset for smoke-test encodes.")
    parser.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of the output video during validation. Use 0 to skip.")
    parser.add_argument("--staging-dir", default=None)
    parser.add_argument("--keep-staging", action="store_true")
    parser.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-progress", action="store_true", help="Do not print live conversion progress.")
    parser.add_argument("--report", default=None, help="Write the full JSON report to this path.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    add_makemkv_args(parser)
    parser.add_argument("--verbose", action="store_true")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run-job":
        if len(argv) != 2:
            print("ERROR: run-job requires a job file", file=sys.stderr)
            return 1
        try:
            return cmd_run_job(argparse.Namespace(job=argv[1]))
        except ToolError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_named_preset_to_args(args)
        if getattr(args, "decode_sample", None) == 0:
            args.decode_sample = None
        return args.func(args)
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
