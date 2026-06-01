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
    DEFAULT_ANIME_CQ_MIN_DURATION,
    DEFAULT_AUDIO_MODE,
    DEFAULT_MAKEMKV_TIMEOUT_SECONDS,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_REPORT_DIR,
    DEFAULT_STEREO_AUDIO_BITRATE,
    DEFAULT_VLC_COMPATIBILITY_MODE,
    HEVC_ENCODERS,
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
from .encoding import encode_to_hevc_m2ts, transcode_compact_audio_tracks
from .muxing import (
    author_m2ts_split,
    author_uhdbd_split,
    write_tsmuxer_meta,
)
from .navigation import (
    patch_navigation_for_hevc,
    restore_source_clpi,
)
from .output import (
    conversion_succeeded,
    copy_disc_tree_skipping_reencoded_streams,
    default_output_for,
    disc_title_from_folder_name,
    ensure_disc_library_metadata,
    make_output_available,
    path_or_none,
    print_conversion_summary,
    safe_name,
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
    require_hevc_encoder,
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
) -> dict[str, Any] | None:
    spec = parse_quality_spec(quality, option="--main-title-quality")
    if spec is None:
        return None
    candidates = reencode_quality_candidates(clips)
    main_clip = candidates[0] if candidates else None
    if not main_clip:
        return None
    report = apply_quality_spec_to_clip(
        main_clip,
        spec,
        bitrate_options or {},
        override_kind="main_title_quality",
        override_label=f"main title quality override to {spec.get('quality')}",
    )
    return report


def apply_main_title_bitrate_mode_override(
    clips: list[dict[str, Any]],
    mode: str | None,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if mode is None:
        return None
    candidates = reencode_quality_candidates(clips)
    main_clip = candidates[0] if candidates else None
    if not main_clip:
        return None
    options = override_bitrate_options(bitrate_options, mode=mode)
    report = retarget_clip(
        main_clip,
        options,
        override_kind="main_title_quality",
        override_label=f"main title bitrate mode override to {normalize_bitrate_mode(mode)}",
    )
    report["mode"] = normalize_bitrate_mode(mode)
    return report


def apply_main_title_cq_override(
    clips: list[dict[str, Any]],
    cq_value: int | None,
    bitrate_options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if cq_value is None:
        return None
    cq_value = validate_cq_value(cq_value, option="--main-title-cq")
    candidates = reencode_quality_candidates(clips)
    main_clip = candidates[0] if candidates else None
    if not main_clip:
        return None
    options = override_bitrate_options(bitrate_options, cq_value=cq_value)
    report = retarget_clip(main_clip, options, override_kind="main_title_cq", override_label=f"main title CQ override to {cq_value}")
    report["cq"] = cq_value
    report["previous_cq"] = report["previous"].get("cq")
    return report


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


def apply_quality_overrides(
    clips: list[dict[str, Any]],
    bitrate_options: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    general_quality_override = apply_general_quality_override(clips, getattr(args, "quality", None), bitrate_options)
    main_title_quality_override = None
    main_title_cq_override = None
    main_title_mode_override = None
    top_n_quality_override = None
    top_n_cq_override = None
    top_n_mode_override = None
    if getattr(args, "main_title_quality", None) is not None:
        main_title_quality_override = apply_main_title_quality_override(clips, getattr(args, "main_title_quality", None), bitrate_options)
    elif getattr(args, "main_title_cq", None) is not None:
        main_title_cq_override = apply_main_title_cq_override(clips, getattr(args, "main_title_cq", None), bitrate_options)
    elif getattr(args, "main_title_bitrate_mode", None) is not None:
        main_title_mode_override = apply_main_title_bitrate_mode_override(clips, getattr(args, "main_title_bitrate_mode", None), bitrate_options)
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
    if getattr(args, "no_makemkv", False):
        raise ToolError("movie-only mode uses MakeMKV title selection; use auto/clone-streams for MakeMKV-free conversion")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve() if args.output else default_output_for(source, "movie-only")
    if not args.dry_run:
        make_output_available(output, source, force=args.force)
    makemkv = run_makemkv_scan(source, tools, verbose=args.verbose)
    title = choose_title(makemkv, args.title)
    staging = Path(args.staging_dir).resolve() if args.staging_dir else Path(tempfile.mkdtemp(prefix=f"{source.name}_bd2uhd_", dir=str(output.parent)))
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
    clip_info = inspect_clip(transcode_input, tools, accurate_video_bitrate=not args.fast_bitrate and not args.dry_run, bitrate_options=bitrate_options)
    if clip_info.get("action") != "reencode" and not args.force_encode:
        raise ToolError(f"Selected title does not require non-HEVC reencoding: {transcode_input}")
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
    validation_source = None if args.skip_audio else (transcode_input if transcode_input.exists() else None)
    validation = validate_clip(
        validation_source,
        output_clip,
        tools,
        decode_seconds=args.decode_sample,
        require_hevc="always",
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
    quality_overrides: dict[str, Any] | None = None,
    copy_clip_overrides: dict[str, Any] | None = None,
    target_disc_fit: dict[str, Any] | None = None,
    planning_pending: bool = False,
) -> dict[str, Any]:
    return {
        "mode": "clone-streams",
        "warning": "full-disc mode preserves the original menu/extras structure and patches replacement-video navigation metadata",
        "source": str(source),
        "output": str(output),
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": selected_hevc_encoder(args),
        "bitrate": bitrate_options,
        "main_title_cq_override": main_title_cq_override,
        "top_n_cq_override": top_n_cq_override,
        "quality_overrides": quality_overrides,
        "copy_clip_overrides": copy_clip_overrides,
        "audio": {
            "mode": audio_mode_from_args(args),
            "stereo_bitrate": stereo_audio_bitrate_from_args(args),
            "mono_bitrate": mono_audio_bitrate_from_args(args),
        },
        "target_disc_fit": target_disc_fit,
        "uhd_profile": getattr(args, "uhd_profile", "auto"),
        "patch_navigation": args.patch_navigation,
        "bdj_compatibility_patches": bool(getattr(args, "bdj_compatibility_patches", False)),
        "vlc_compatibility": getattr(args, "vlc_compat", DEFAULT_VLC_COMPATIBILITY_MODE),
        "vlc_fixes": compatibility_fix_names_from_args(args),
        "custom_compatibility_patch_files": [str(path) for path in custom_compatibility_patch_files_from_args(args)],
        "encode_ahead": encoder_is_hardware(selected_hevc_encoder(args)) and not getattr(args, "no_encode_ahead", False),
        "encode_ahead_depth": getattr(args, "encode_ahead_depth", 3),
        "planning_pending": planning_pending,
        "reencode_clips": [clip_summary(c) for c in clips],
    }


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
    clpi_report = restore_source_clpi(ctx["input_path"], ctx["output_clpi"], output_clip=ctx["output_path"])
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
    encoded_queue: thread_queue.Queue[tuple[str, dict[str, Any] | BaseException | None]] = thread_queue.Queue(maxsize=max_depth)
    audio_ready_queue: thread_queue.Queue[tuple[str, dict[str, Any] | BaseException | None]] = thread_queue.Queue(maxsize=max_depth)
    stop_event = threading.Event()

    def queue_put(q: thread_queue.Queue[tuple[str, dict[str, Any] | BaseException | None]], item: tuple[str, dict[str, Any] | BaseException | None]) -> bool:
        while not stop_event.is_set():
            try:
                q.put(item, timeout=0.5)
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
                if not queue_put(encoded_queue, ("encoded", ctx)):
                    break
        except BaseException as exc:
            queue_put(encoded_queue, ("error", exc))
        finally:
            queue_put(encoded_queue, ("done", None))

    def audio_worker() -> None:
        try:
            while True:
                kind, payload = encoded_queue.get()
                if kind == "done":
                    queue_put(audio_ready_queue, ("done", None))
                    return
                if kind == "error":
                    queue_put(audio_ready_queue, ("error", payload))
                    return
                ctx = payload
                assert isinstance(ctx, dict)
                transcode_compact_audio_context(ctx, tools, args)
                if not queue_put(audio_ready_queue, ("audio", ctx)):
                    return
        except BaseException as exc:
            queue_put(audio_ready_queue, ("error", exc))

    progress_event("pipeline", "enabled", mode="encode-audio-mux-queue", depth=max_depth)
    with ThreadPoolExecutor(max_workers=2) as executor:
        producer_future = executor.submit(producer)
        audio_future = executor.submit(audio_worker)
        try:
            while True:
                kind, payload = audio_ready_queue.get()
                if kind == "done":
                    audio_future.result()
                    producer_future.result()
                    break
                if kind == "error":
                    stop_event.set()
                    audio_future.result()
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
                    stage="muxing audio queue",
                    enabled=progress_enabled,
                )
                validation = mux_validate_clone_clip_context(ctx, tools, args)
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
            for q in (encoded_queue, audio_ready_queue):
                while True:
                    try:
                        q.get_nowait()
                    except thread_queue.Empty:
                        break
                try:
                    q.put_nowait(("done", None))
                except thread_queue.Full:
                    pass
            raise
    return validations, done_seconds


def convert_clone_streams(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if args.uhd_scale or args.skip_audio or args.skip_subtitles:
        raise ToolError("--uhd-scale, --skip-audio, and --skip-subtitles are only supported by movie-only mode")
    validate_cq_override_args(args)
    source = Path(args.source).resolve()
    output = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    bitrate_options = bitrate_options_for_args(args)
    if not args.dry_run:
        make_output_available(output, source, force=args.force)
    scan = scan_disc(
        source,
        tools,
        accurate_video_bitrate=not args.fast_bitrate and not args.dry_run,
        bitrate_options=bitrate_options,
        use_makemkv=use_makemkv_from_args(args),
        verbose=args.verbose,
    )
    remember_original_clip_actions(scan.get("clips", []))
    quality_overrides = apply_quality_overrides(scan.get("clips", []), bitrate_options, args)
    main_title_cq_override = (quality_overrides or {}).get("main_title_cq")
    top_n_cq_override = (quality_overrides or {}).get("top_n_cq")
    copy_clip_overrides = apply_clip_copy_overrides(scan.get("clips", []), getattr(args, "copy_clips", None))
    disc_fit = fit_reencoded_clips_to_disc_size(
        source,
        scan.get("clips", []),
        target_size=getattr(args, "target_disc_size", None),
        margin=getattr(args, "target_disc_margin", 0.98),
        audio_mode=audio_mode_from_args(args),
    )
    clips = [c for c in scan.get("clips", []) if c.get("action") == "reencode"]
    progress_plan_path = path_or_none(getattr(args, "progress_plan", None))
    plan_payload = clone_streams_plan_payload(
        source,
        output,
        args,
        bitrate_options,
        main_title_cq_override,
        top_n_cq_override,
        clips,
        quality_overrides=quality_overrides,
        copy_clip_overrides=copy_clip_overrides,
        target_disc_fit=disc_fit,
    )
    if progress_plan_path:
        progress_plan_path.parent.mkdir(parents=True, exist_ok=True)
        save_job(progress_plan_path, plan_payload)
    if args.dry_run:
        return plan_payload
    copy_report = copy_disc_tree_skipping_reencoded_streams(source, output, {str(clip.get("file")) for clip in clips})
    disc_metadata = ensure_disc_library_metadata(output)
    validations = []
    total_seconds = sum(float(clip.get("duration") or 0) for clip in clips)
    done_seconds = 0.0
    progress_enabled = not getattr(args, "no_progress", False)
    contexts = [clone_clip_context(source, output, clip) for clip in clips]
    encoder = selected_hevc_encoder(args)
    encode_ahead = len(contexts) > 1 and encoder_is_hardware(encoder) and not getattr(args, "no_encode_ahead", False)
    compact_audio_pipeline = encode_ahead and audio_mode_from_args(args) == "compact-stereo"
    if encode_ahead:
        if compact_audio_pipeline:
            validations, done_seconds = run_queued_encode_audio_mux_pipeline(
                contexts,
                tools,
                args,
                total_seconds=total_seconds,
                progress_enabled=progress_enabled,
            )
        else:
            validations, done_seconds = run_queued_encode_mux_pipeline(
                contexts,
                tools,
                args,
                total_seconds=total_seconds,
                progress_enabled=progress_enabled,
            )
    else:
        for index, ctx in enumerate(contexts, start=1):
            emit_conversion_progress(
                done_seconds,
                total_seconds,
                len(validations),
                len(clips),
                current=ctx["file"],
                stage="encoding",
                enabled=progress_enabled,
            )
            encode_clone_clip_context(ctx, tools, args)
            validation = finalize_clone_clip_context(ctx, tools, args)
            validations.append(validation)
            done_seconds += float(ctx["clip"].get("duration") or 0)
            emit_conversion_progress(
                done_seconds,
                total_seconds,
                index,
                len(clips),
                current=ctx["file"],
                stage="validated",
                enabled=progress_enabled,
            )
    navigation_patch = None
    emit_conversion_progress(
        done_seconds,
        total_seconds,
        len(clips),
        len(clips),
        current=None,
        stage="post-processing",
        enabled=progress_enabled,
    )
    if args.patch_navigation:
        navigation_patch = patch_navigation_for_hevc(output, [clip["file"] for clip in clips], tools=tools, source_root=source)
    uhd_structure = None
    if getattr(args, "uhd_profile", "auto") != "off":
        uhd_structure = ensure_uhd_backup_structure(output)
    bdj_compatibility_patch = None
    selected_vlc_fixes = compatibility_fix_names_from_args(args)
    custom_patch_files = custom_compatibility_patch_files_from_args(args)
    if selected_vlc_fixes or custom_patch_files:
        bdj_compatibility_patch = patch_known_bdj_compatibility(
            output,
            fixes=selected_vlc_fixes,
            custom_patch_files=custom_patch_files,
        )
    makemkv_validation = validate_disc_titles(
        output,
        tools,
        use_makemkv=use_makemkv_from_args(args),
        require_makemkv=getattr(args, "require_makemkv", False),
        verbose=args.verbose,
    )
    result = {
        "mode": "clone-streams",
        "warning": "full-disc mode preserves BD-J/menu structure; patched navigation metadata still needs player testing",
        "source": str(source),
        "output": str(output),
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": encoder,
        "bitrate": bitrate_options,
        "main_title_cq_override": main_title_cq_override,
        "top_n_cq_override": top_n_cq_override,
        "quality_overrides": quality_overrides,
        "copy_clip_overrides": copy_clip_overrides,
        "audio": {
            "mode": audio_mode_from_args(args),
            "stereo_bitrate": stereo_audio_bitrate_from_args(args),
            "mono_bitrate": mono_audio_bitrate_from_args(args),
        },
        "vlc_compatibility": getattr(args, "vlc_compat", DEFAULT_VLC_COMPATIBILITY_MODE),
        "target_disc_fit": disc_fit,
        "uhd_structure": uhd_structure,
        "vlc_fixes": selected_vlc_fixes,
        "custom_compatibility_patch_files": [str(path) for path in custom_patch_files],
        "encode_ahead": encode_ahead,
        "encode_ahead_depth": getattr(args, "encode_ahead_depth", 3) if encode_ahead else 0,
        "preservation_copy": copy_report,
        "disc_metadata": disc_metadata,
        "reencoded": [c["file"] for c in clips],
        "validation": validations,
        "makemkv_validation": makemkv_validation,
    }
    if encode_ahead:
        result["pipeline"] = "encode-audio-mux-queue" if compact_audio_pipeline else "encode-mux-queue"
    if navigation_patch is not None:
        result["navigation_patch"] = navigation_patch
    if bdj_compatibility_patch is not None:
        result["bdj_compatibility_patch"] = bdj_compatibility_patch
    emit_conversion_progress(
        done_seconds,
        total_seconds,
        len(clips),
        len(clips),
        current=None,
        stage="completed",
        enabled=progress_enabled,
    )
    return result


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
            bitrate_options=bitrate_options,
            use_makemkv=use_makemkv_from_args(args),
            verbose=args.verbose,
        )
        remember_original_clip_actions(report.get("clips", []))
        report["quality_overrides"] = apply_quality_overrides(report.get("clips", []), bitrate_options, args)
        report["copy_clip_overrides"] = apply_clip_copy_overrides(report.get("clips", []), getattr(args, "copy_clips", None))
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
    action = clip.get("action")
    original_action = original_clip_action(clip)
    video = clip.get("video") or {}
    target = video.get("target_hevc") or {}
    if action == "copy":
        return "copy" if original_action == "reencode" else str(original_action or "copy")
    if action == "already_hevc":
        return "already HEVC"
    if action != "reencode":
        return str(action or "")
    mode = target.get("mode") or "balanced"
    if target.get("rate_control") == "cq":
        return f"cq:{target.get('cq')} ({mode})"
    if target.get("target_mbps") is not None:
        return f"{target.get('target_mbps')} Mbps ({mode})"
    return str(mode)


def planned_clip_output_codec(clip: dict[str, Any]) -> str | None:
    video = clip.get("video") or {}
    source_codec = video.get("codec_name")
    action = clip.get("action")
    if action == "reencode":
        return "hevc"
    if action in {"copy", "already_hevc"}:
        return source_codec
    return None


def clip_list_rows(clips: list[dict[str, Any]], *, sort: str = "duration") -> list[dict[str, Any]]:
    if sort == "file":
        sorted_clips = sorted(clips, key=lambda item: str(item.get("file") or ""))
    else:
        sorted_clips = sorted(clips, key=lambda item: float(item.get("duration") or 0), reverse=True)
    rows: list[dict[str, Any]] = []
    for clip in sorted_clips:
        video = clip.get("video") or {}
        rows.append(
            {
                "clip": clip.get("file"),
                "duration": clip.get("duration"),
                "duration_text": format_duration(clip.get("duration")),
                "planned_action": clip.get("action"),
                "original_action": original_clip_action(clip),
                "codec": video.get("codec_name"),
                "source_codec": video.get("codec_name"),
                "planned_codec": planned_clip_output_codec(clip),
                "source_video_mbps": video.get("source_video_bitrate_mbps"),
                "planned_quality": planned_clip_quality_text(clip),
            }
        )
    return rows


def print_clip_list(rows: list[dict[str, Any]]) -> None:
    print(f"{'clip':<12} {'duration':>8} {'action':<12} {'source':<10} {'output':<8} {'src Mbps':>8}  quality")
    print(f"{'-' * 12} {'-' * 8} {'-' * 12} {'-' * 10} {'-' * 8} {'-' * 8}  {'-' * 24}")
    for row in rows:
        mbps_text = "" if row.get("source_video_mbps") is None else str(row.get("source_video_mbps"))
        print(
            f"{str(row.get('clip') or ''):<12} "
            f"{str(row.get('duration_text') or ''):>8} "
            f"{str(row.get('planned_action') or ''):<12} "
            f"{str(row.get('source_codec') or row.get('codec') or ''):<10} "
            f"{str(row.get('planned_codec') or ''):<8} "
            f"{mbps_text:>8}  "
            f"{row.get('planned_quality') or ''}"
        )


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
        bitrate_options=bitrate_options,
        use_makemkv=use_makemkv_from_args(args),
        verbose=args.verbose,
    )
    remember_original_clip_actions(report.get("clips", []))
    quality_overrides = apply_quality_overrides(report.get("clips", []), bitrate_options, args)
    copy_clip_overrides = apply_clip_copy_overrides(report.get("clips", []), getattr(args, "copy_clips", None))
    rows = clip_list_rows(report.get("clips", []), sort=args.sort)
    payload = {
        "source": str(source),
        "sort": args.sort,
        "bitrate": bitrate_options,
        "quality_overrides": quality_overrides,
        "copy_clip_overrides": copy_clip_overrides,
        "clips": rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"BD2HEVC clips for {source.name}")
        print_clip_list(rows)
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    if args.mode == "movie-only":
        result = convert_movie_only(args, tools)
    else:
        result = convert_clone_streams(args, tools)
    report_path = path_or_none(getattr(args, "report", None))
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print_conversion_summary(result, report_path=report_path, dry_run=args.dry_run)
    return 0 if conversion_succeeded(result, require_makemkv=getattr(args, "require_makemkv", False), dry_run=args.dry_run) else 4


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
    results = []
    for clip in clips:
        if not clip.exists():
            continue
        reference_clip = reference_stream_dir / clip.name if reference_stream_dir else None
        results.append(validate_clip(reference_clip, clip, tools, decode_seconds=args.decode_sample, require_hevc=require_hevc))
    payload: dict[str, Any] = {"source_backup": args.source_backup, "reference": args.reference, "clips": results}
    if makemkv_validation is not None:
        payload["makemkv_validation"] = makemkv_validation
    ok = all(r.get("ok") for r in results) and (makemkv_validation is None or makemkv_validation.get("ok") or not args.require_makemkv)
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
    reports = [ensure_uhd_backup_structure(root) for root in roots]
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
            print(f"  Patched version headers: {version_patches}")
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
    report = patch_navigation_for_hevc(roots[0], clips, tools=tools, source_root=source_root, refresh_cpi=args.refresh_cpi, verbose=args.verbose)
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
    uri = "bluray:///" + root.as_posix()
    cmd = [
        vlc,
        "-I",
        "dummy",
        "--dummy-quiet",
        "--no-qt-privacy-ask",
        "--play-and-exit",
        f"--run-time={args.seconds}",
        "--file-logging",
        f"--logfile={log_path}",
        "--verbose=2",
        "--bluray-menu",
        uri,
    ]
    if not args.allow_resume:
        cmd[5:5] = ["--qt-continue=0"]
    if args.d3d11:
        cmd[5:5] = ["--avcodec-hw=d3d11va"]
    if args.video_plane:
        cmd[5:5] = ["--vout", "dummy", "--aout", "dummy"]
    else:
        cmd[5:5] = ["--no-video", "--aout", "dummy"]
    proc = run_cmd(cmd, check=False, capture=True, timeout_seconds=args.seconds + 30, verbose=args.verbose)
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
    result = {
        "target": str(root),
        "mode": "headless-dummy-video" if args.video_plane else "headless-no-video",
        "note": "This avoids visible windows. Dummy-video mode exercises VLC's video/subpicture path, but it still cannot prove visual menu overlay state. VLC may ignore --run-time on BD-J menus, so timeout is acceptable after a clean open.",
        "command": format_cmd(cmd),
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
                "ok": (not args.video_plane) or counters["adding_es"] > 0 or counters["reusing_es"] > 0,
                "adding_es": counters["adding_es"],
                "reusing_es": counters["reusing_es"],
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


def enqueue_conversion_job(args: argparse.Namespace, *, announce: bool = True) -> dict[str, Any]:
    validate_cq_override_args(args)
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
    validate_encoder_bitrate_compatibility(args)
    source = Path(args.source).resolve()
    roots = find_disc_roots([source])
    if not roots:
        raise ToolError(f"No BDMV folder found at {source}")
    source = roots[0]
    output = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    make_output_available(output, source, force=args.force)

    job_id = safe_name(args.name or f"{time.strftime('%Y%m%d-%H%M%S')}-{source.name}")
    paths = job_paths(job_id)
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
        "plan": str(paths["plan"]),
        "log": str(paths["log"]),
        "report": str(paths["report"]),
        "exitcode": str(paths["exitcode"]),
        "job_file": str(paths["job"]),
        "command": command,
        "reencode_clip_count": None,
    }
    save_job(paths["job"], job)
    pid = start_background_process(paths["job"])
    job["status"] = "queued"
    job["pid"] = pid
    job["queued_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_job(paths["job"], job)
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
        name = f"{disc_title_from_folder_name(source.name)} (BD) (UHD converted)"
        return Path(args.output_dir).resolve() / name
    return default_output_for(source, "clone-streams")


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
    parser.add_argument("--compact-cq-value", "--anime-cq-value", dest="compact_cq_value", type=int, default=ANIME_CQ_VALUE, help="CQ value for reencoded clips when --bitrate-mode compact-cq is used. Lower is larger/higher quality; default 18.")
    parser.add_argument("--compact-cq-min-duration", "--episode-compact-min-duration", "--anime-cq-min-duration", dest="anime_cq_min_duration", type=parse_duration_arg, default=DEFAULT_ANIME_CQ_MIN_DURATION, help="Minimum clip duration for --bitrate-mode compact-cq to use CQ. Defaults to the 10-second reencode threshold. Raise it if only episode/movie-length clips should use CQ. Accepts values like 15m or 00:15:00. Shorter reencoded clips use smaller. --episode-compact-min-duration and --anime-cq-min-duration are accepted as legacy aliases.")
    parser.add_argument("--main-title-quality", metavar="QUALITY", default=None, help="Quality for the longest reencode-eligible clip. Accepts a bitrate preset, cq:N, source-ratio:N, legacy presets, or copy/no-reencode. Mutually exclusive with top-N overrides.")
    parser.add_argument("--main-title-bitrate-mode", metavar="MODE", default=None, help="Legacy spelling for --main-title-quality MODE.")
    parser.add_argument("--main-title-cq", type=int, default=None, help="Use compact-cq at this CQ value for the longest reencoded clip. Lower is larger/higher quality; useful for CQ20 extras with a CQ18 main movie.")
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
        parser.add_argument("--encode-ahead-depth", type=int, default=3, help="Maximum completed HEVC temp clips allowed to wait for later audio/muxing stages. Hardware encoders only; default 3.")


def add_audio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio-mode", choices=AUDIO_MODES, default=DEFAULT_AUDIO_MODE, help="Audio handling for reencoded clips. passthrough keeps source audio; compact-stereo converts every audio track to AC-3 stereo, or mono when the source stream is mono.")
    parser.add_argument("--stereo-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_STEREO_AUDIO_BITRATE, help="Bitrate for compact-stereo two-channel AC-3 audio. Default 256k.")
    parser.add_argument("--mono-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_MONO_AUDIO_BITRATE, help="Bitrate for compact-stereo mono AC-3 audio. Default 128k.")


def add_uhd_output_args(parser: argparse.ArgumentParser) -> None:
    sizes = ", ".join(DISC_SIZE_BYTES)
    parser.add_argument("--uhd-profile", choices=["auto", "off"], default="auto", help="Patch output structure toward UHD-BD folder conventions: UHD navigation versions, backup mirrors, and required folder placeholders. Default auto.")
    parser.add_argument("--target-disc-size", default=None, metavar="SIZE", help=f"Scale VBR video targets to fit a physical-disc budget. Accepts {sizes}, or a size such as 23.5GB. Requires VBR targets, not CQ.")
    parser.add_argument("--target-disc-margin", type=float, default=0.98, help="Safety margin for --target-disc-size. Default 0.98 leaves room for filesystem/authoring overhead.")


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
    parser = argparse.ArgumentParser(
        description="BD2HEVC: convert local Blu-ray backups to HEVC while preserving menus, extras, audio, and subtitles.",
        epilog=(
            "Common commands:\n"
            "  py bd2hevc.py queue \"BD backups\" --output-dir \"Converted UHD-BD\"\n"
            "  py bd2hevc.py status --watch\n"
            "  py bd2hevc.py clips \"BD backups\\Movie Disc\"\n"
            "  py bd2hevc.py preset list\n"
            "  py bd2hevc.py jobs\n"
            "  py bd2hevc.py diagnose \"Converted UHD-BD\\Movie (BD) (UHD converted)\"\n"
            "\n"
            "Command help:\n"
            "  py bd2hevc.py <command> --help\n"
            "  py bd2hevc.py queue --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"BD2HEVC {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tools = command_parser(sub, "tools", help="Show discovered external tools and HEVC encoder support.", description="Show the external programs BD2HEVC found and whether hardware HEVC encoding is available.", examples="""
  py bd2hevc.py tools
  py bd2hevc.py tools --json
""")
    p_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools.set_defaults(func=cmd_tools)

    p_preset = command_parser(sub, "preset", help="Save, list, show, and remove named presets.", description="Manage named conversion presets stored in the user config folder.", examples="""
  py bd2hevc.py preset save sarah --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
  py bd2hevc.py preset save source-mix --quality source-ratio:0.60 --codec-source-ratio h264=0.55 --codec-source-ratio mpeg2video=0.30
  py bd2hevc.py preset list
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD" --preset sarah
""")
    preset_sub = p_preset.add_subparsers(dest="preset_command", required=True)
    p_preset_list = preset_sub.add_parser("list", help="List saved and bundled presets.")
    p_preset_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_list.set_defaults(func=cmd_preset_list)

    p_preset_show = preset_sub.add_parser("show", help="Show one preset.")
    p_preset_show.add_argument("name", help="Preset name.")
    p_preset_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_show.set_defaults(func=cmd_preset_show)

    p_preset_save = preset_sub.add_parser("save", help="Save a named preset from command-line options.")
    p_preset_save.add_argument("name", help="Preset name. Use letters, numbers, dots, underscores, and hyphens.")
    p_preset_save.add_argument("--description", default=None, help="Optional short note shown by 'preset list'.")
    p_preset_save.add_argument("--force", action="store_true", help="Replace an existing preset.")
    p_preset_save.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_save.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc", help="Save a preferred HEVC encoder in the preset.")
    p_preset_save.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="Save a preferred HEVC output bit depth.")
    add_bitrate_args(p_preset_save, include_named_preset=False, include_file_preset=False)
    add_audio_args(p_preset_save)
    p_preset_save.set_defaults(func=cmd_preset_save_validated)

    p_preset_remove = preset_sub.add_parser("remove", aliases=["rm", "delete"], help="Remove a user preset.")
    p_preset_remove.add_argument("name", help="Preset name.")
    p_preset_remove.set_defaults(func=cmd_preset_remove)

    p_scan = command_parser(sub, "scan", help="Scan one or more BDMV backups with MakeMKV and FFprobe.", description="Inspect Blu-ray backup folders before conversion and write scan reports.", examples="""
  py bd2hevc.py scan "BD backups\\Movie Disc"
  py bd2hevc.py scan "BD backups" --no-makemkv
  py bd2hevc.py scan "BD backups\\Movie Disc" --accurate-video-bitrate
""")
    p_scan.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_scan.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p_scan.add_argument("--accurate-video-bitrate", action="store_true", help="Sum video packet sizes for bitrate. Slower, but best for encode planning.")
    add_bitrate_args(p_scan)
    add_makemkv_args(p_scan)
    p_scan.add_argument("--verbose", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_clips = command_parser(sub, "clips", help="List M2TS clip names, durations, and planned quality.", description="List the source clips in a Blu-ray backup so quality overrides can be chosen without reading raw JSON.", examples="""
  py bd2hevc.py clips "BD backups\\Movie Disc"
  py bd2hevc.py clips "BD backups\\Episode Disc" --quality cq:20 --top-n-quality 3 cq:18
  py bd2hevc.py clips "BD backups\\Menu-heavy Disc" --sort file --clip-quality 00012 copy
""")
    p_clips.add_argument("source", help="Source BD backup folder.")
    p_clips.add_argument("--sort", choices=["duration", "file"], default="duration", help="Sort by duration descending or by clip filename.")
    p_clips.add_argument("--accurate-video-bitrate", action="store_true", help="Sum video packet sizes for bitrate. Slower, but best for exact source Mbps.")
    add_bitrate_args(p_clips)
    add_makemkv_args(p_clips)
    p_clips.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_clips.add_argument("--verbose", action="store_true")
    p_clips.set_defaults(func=cmd_clips)

    p_convert = command_parser(sub, "convert", help="Convert a BD backup.", description="Legacy conversion command. For normal full-disc menu-preserving use, prefer 'auto', 'start', or 'queue'.", examples="""
  py bd2hevc.py convert "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc"
  py bd2hevc.py convert "BD backups\\Movie Disc" --mode clone-streams
  py bd2hevc.py convert "BD backups\\Movie Disc" --mode movie-only --title 0
""")
    add_convert_args(p_convert)
    p_convert.set_defaults(func=cmd_convert)

    p_auto = command_parser(sub, "auto", help="Faithful full-disc conversion. Only the source backup path is required.", description="Run a foreground full-disc conversion that preserves menus, extras, subtitles, and audio by default.", examples="""
  py bd2hevc.py auto "BD backups\\Movie Disc"
  py bd2hevc.py auto "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py auto "BD backups\\Movie Disc" --encoder libx265
  py bd2hevc.py auto "BD backups\\Movie Disc" --quality cq:20 --audio-mode compact-stereo
""")
    p_auto.add_argument("source", help="Source BD backup folder.")
    p_auto.add_argument("output", nargs="?", default=None, help="Output folder. Defaults to <source>_FULL_DISC_HEVC.")
    p_auto.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_auto.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_auto.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth. 8 preserves 8-bit BD sources and is VLC-friendly; use 10 for explicit Main10 output.")
    add_encoder_args(p_auto, include_encode_ahead=True)
    add_bitrate_args(p_auto)
    add_audio_args(p_auto)
    add_uhd_output_args(p_auto)
    p_auto.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_auto.add_argument("--progress-plan", default=None, help=argparse.SUPPRESS)
    p_auto.add_argument("--staging-dir", default=None)
    p_auto.add_argument("--keep-staging", action="store_true")
    p_auto.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    p_auto.add_argument("--dry-run", action="store_true")
    p_auto.add_argument("--no-progress", action="store_true", help="Do not print live conversion progress.")
    p_auto.add_argument("--report", default=None, help="Write the full JSON report to this path.")
    p_auto.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    add_makemkv_args(p_auto)
    p_auto.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_auto.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(p_auto)
    p_auto.add_argument("--verbose", action="store_true")
    p_auto.set_defaults(func=cmd_auto)

    p_start = command_parser(sub, "start", help="Start a full-disc conversion in the background.", description="Start one background conversion job and return immediately with status commands.", examples="""
  py bd2hevc.py start "BD backups\\Movie Disc" --name Movie_Disc
  py bd2hevc.py start "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py start "BD backups\\Movie Disc" --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
""")
    p_start.add_argument("source", help="Source BD backup folder.")
    p_start.add_argument("output", nargs="?", default=None, help="Output folder. Defaults next to the source.")
    p_start.add_argument("--name", default=None, help="Friendly job id. Defaults to timestamp plus source folder name.")
    p_start.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_start.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_start.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_start, include_encode_ahead=True)
    add_bitrate_args(p_start)
    add_audio_args(p_start)
    add_uhd_output_args(p_start)
    p_start.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_start.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    add_makemkv_args(p_start)
    p_start.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_start.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(p_start)
    p_start.add_argument("--verbose", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_queue = command_parser(sub, "queue", help="Queue multiple full-disc conversions that run one at a time.", description="Queue one or more source folders. Jobs run one at a time in the background.", examples="""
  py bd2hevc.py queue "BD backups\\Movie Disc" --output-dir "Converted UHD-BD"
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD"
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD" --encoder libx265
  py bd2hevc.py queue "Disc 1" "Disc 2" --output-dir "Converted UHD-BD" --quality cq:20
  py bd2hevc.py queue "Movie Disc" --output-dir "Converted UHD-BD" --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
  py bd2hevc.py queue "Episode Disc" --output-dir "Converted UHD-BD" --quality cq:20 --top-n-quality 3 cq:18
""")
    p_queue.add_argument("sources", nargs="+", help="Source BD backup folders or parent folders containing BDMV backups.")
    p_queue.add_argument("--output-dir", default=None, help="Put each converted output in this folder using '<Title> (BD) (UHD converted)' names.")
    p_queue.add_argument("--name-prefix", default=None, help="Prefix for generated job ids. Defaults to the current timestamp.")
    p_queue.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_queue.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_queue.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_queue, include_encode_ahead=True)
    add_bitrate_args(p_queue)
    add_audio_args(p_queue)
    add_uhd_output_args(p_queue)
    p_queue.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_queue.add_argument("--force", action="store_true", help="Replace existing output folders.")
    add_makemkv_args(p_queue)
    p_queue.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_queue.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(p_queue)
    p_queue.add_argument("--verbose", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_status = command_parser(sub, "status", help="Show progress for a background conversion.", description="Show progress for the current job, a specific job, or the whole queue when watched without a job id.", examples="""
  py bd2hevc.py status
  py bd2hevc.py status --watch
  py bd2hevc.py status 20260528-My_Movie --watch
  py bd2hevc.py status 20260528-My_Movie --watch 5
""")
    p_status.add_argument("job", nargs="?", default=None, help="Job id, output folder, or source folder. Defaults to the newest job.")
    p_status.add_argument("--watch", nargs="?", const=1.0, type=float, default=0, help="Refresh every N seconds. Defaults to 1 second when no interval is supplied.")
    p_status.add_argument("--width", type=int, default=32)
    p_status.set_defaults(func=cmd_status)

    p_jobs = command_parser(sub, "jobs", help="List recent background conversions.", description="List running, queued, completed, failed, and canceled background jobs.", examples="""
  py bd2hevc.py jobs
  py bd2hevc.py jobs --limit 30
  py bd2hevc.py jobs --active
  py bd2hevc.py jobs --failed --hide-old-failed
""")
    p_jobs.add_argument("--limit", type=int, default=10)
    p_jobs.add_argument("--active", action="store_true", help="Show only running, queued, and paused jobs.")
    p_jobs.add_argument("--failed", action="store_true", help="Show only failed jobs.")
    p_jobs.add_argument("--completed", action="store_true", help="Show only completed jobs.")
    p_jobs.add_argument("--canceled", action="store_true", help="Show only canceled jobs.")
    p_jobs.add_argument("--hide-old-failed", action="store_true", help="Hide failed jobs when a newer completed job has the same output folder.")
    p_jobs.set_defaults(func=cmd_jobs)

    p_pause = command_parser(sub, "pause-queue", help="Pause the background queue after the current running job.", description="Pause queued jobs. The currently running conversion is allowed to continue.", examples="""
  py bd2hevc.py pause-queue
  py bd2hevc.py pause-queue --reason "Need the GPU for something else"
""")
    p_pause.add_argument("--reason", default=None, help="Optional note saved with the pause marker.")
    p_pause.set_defaults(func=cmd_pause_queue)

    p_resume = command_parser(sub, "resume-queue", help="Resume a paused background queue.", description="Resume jobs that were paused with pause-queue.", examples="""
  py bd2hevc.py resume-queue
""")
    p_resume.set_defaults(func=cmd_resume_queue)

    p_cancel = command_parser(sub, "cancel", help="Cancel a queued job. Use --kill to stop a running job.", description="Cancel a queued conversion. Use --kill only when you really want to stop a running conversion process.", examples="""
  py bd2hevc.py cancel 20260528-My_Movie
  py bd2hevc.py cancel "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py cancel 20260528-My_Movie --kill
""")
    p_cancel.add_argument("job", help="Job id, output folder, or source folder.")
    p_cancel.add_argument("--kill", action="store_true", help="Stop a running conversion process tree.")
    p_cancel.set_defaults(func=cmd_cancel_job)

    p_remove = command_parser(sub, "remove", help="Remove a job from the queue/status list without deleting converted output.", description="Hide an old job from BD2HEVC's job list. This does not delete the converted backup.", examples="""
  py bd2hevc.py remove 20260528-My_Movie
  py bd2hevc.py remove 20260528-My_Movie --kill
""")
    p_remove.add_argument("job", help="Job id, output folder, or source folder.")
    p_remove.add_argument("--kill", action="store_true", help="Allow removal of a running job by stopping it first.")
    p_remove.set_defaults(func=cmd_remove_job)

    p_validate = command_parser(sub, "validate", help="Validate an output clip or BDMV folder.", description="Run structural and decode checks against an output clip, converted backup, or source backup.", examples="""
  py bd2hevc.py validate "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py validate "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --reference "BD backups\\Movie Disc"
  py bd2hevc.py validate "BD backups\\Movie Disc" --source-backup
""")
    p_validate.add_argument("target")
    p_validate.add_argument("--source-backup", action="store_true", help="Validate a source BD backup without requiring HEVC output clips.")
    p_validate.add_argument("--reference", default=None, help="Original BD backup folder to compare matching stream audio and timestamps against.")
    p_validate.add_argument("--decode-sample", type=float, default=None, help="Decode the first N seconds of video to null.")
    p_validate.add_argument("--report", default=None, help="Write the full JSON validation report to this path.")
    p_validate.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    add_makemkv_args(p_validate)
    p_validate.add_argument("--verbose", action="store_true")
    p_validate.set_defaults(func=cmd_validate)

    p_diagnose = command_parser(sub, "diagnose", help="Create a redacted support bundle.", description="Create a shareable diagnostic zip with redacted logs, tool versions, validation output, and file manifests. Media files and raw disc assets are not included.", examples="""
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --source "BD backups\\Movie Disc"
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --job 20260429-153012-Movie_Disc
""")
    p_diagnose.add_argument("target", help="Converted output folder, source backup folder, or clip to summarize.")
    p_diagnose.add_argument("--source", default=None, help="Original source backup for reference validation and comparison.")
    p_diagnose.add_argument("--job", default=None, help="Matching background job id or prefix when auto-detection is not enough.")
    p_diagnose.add_argument("--output", default=None, help="Destination zip or folder. Defaults to reports/diagnostics/<disc>-<timestamp>.zip.")
    p_diagnose.add_argument("--log-lines", type=int, default=DEFAULT_DIAGNOSTIC_LOG_LINES, help=f"Number of job log lines to include from the end of the log. Default {DEFAULT_DIAGNOSTIC_LOG_LINES}.")
    p_diagnose.add_argument("--no-validation", action="store_true", help="Skip the lightweight no-MakeMKV validation pass.")
    p_diagnose.add_argument("--no-zip", action="store_true", help="Write an unpacked diagnostic folder instead of a zip file.")
    p_diagnose.add_argument("--json", action="store_true", help="Print machine-readable command output.")
    p_diagnose.set_defaults(func=cmd_diagnose)

    p_playlist = command_parser(sub, "playlist-probe", help="Probe a Blu-ray playlist through libbluray/FFprobe and fail on stale CLPI packet maps.", description="Probe one MPLS playlist through libbluray/FFprobe, useful when VLC progress or seeking looks wrong.", examples="""
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23 --reference "BD backups\\Movie Disc"
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23 --count-frames --decode-seconds 30
""")
    p_playlist.add_argument("target", help="BD/UHD-BD backup folder.")
    p_playlist.add_argument("--playlist", type=int, required=True, help="MPLS playlist number, e.g. 23 for 00023.mpls.")
    p_playlist.add_argument("--reference", default=None, help="Original BD backup folder to compare playlist duration against.")
    p_playlist.add_argument("--reference-tolerance", type=float, default=2.0, help="Allowed duration difference from --reference, in seconds.")
    p_playlist.add_argument("--min-duration", type=float, default=None)
    p_playlist.add_argument("--max-duration", type=float, default=None)
    p_playlist.add_argument("--count-frames", action="store_true", help="Ask FFprobe to count decoded video frames.")
    p_playlist.add_argument("--min-video-frames", type=int, default=None, help="Require at least this many decoded video frames.")
    p_playlist.add_argument("--decode-seconds", type=float, default=None, help="Decode this many seconds of playlist video with FFmpeg.")
    p_playlist.add_argument("--allow-eof", action="store_true", help="Do not fail when libbluray reports Read past EOF.")
    p_playlist.add_argument("--report", default=None, help="Optional JSON report path.")
    p_playlist.set_defaults(func=cmd_playlist_probe)

    p_metadata = command_parser(sub, "patch-disc-metadata", help="Create fallback BD disc-library metadata when a backup is missing it.", description="Create simple BD disc-library metadata so VLC shows a disc title instead of a file URL.", examples="""
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD" --force
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --title "Movie Disc"
""")
    p_metadata.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_metadata.add_argument("--title", default=None, help="Use this title for every patched disc. Defaults to a cleaned folder name.")
    p_metadata.add_argument("--force", action="store_true", help="Overwrite existing bdmt_*.xml metadata.")
    p_metadata.set_defaults(func=cmd_patch_disc_metadata)

    p_uhd_profile = command_parser(sub, "patch-uhd-profile", help="Patch an existing output toward UHD-BD folder conventions.", description="Create expected UHD-BD-style folders, mirror required backup files, and update copied Blu-ray navigation version headers where applicable.", examples="""
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD"
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --json
""")
    p_uhd_profile.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_uhd_profile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_uhd_profile.set_defaults(func=cmd_patch_uhd_profile)

    p_patch = command_parser(sub, "patch-navigation", help="Patch full-disc CLPI/MPLS descriptors for HEVC replacement clips.", description="Patch Blu-ray navigation metadata after HEVC replacement so players see the new video streams correctly.", examples="""
  py bd2hevc.py patch-navigation "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --reference "BD backups\\Movie Disc"
  py bd2hevc.py patch-navigation "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 00002
""")
    p_patch.add_argument("target")
    p_patch.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to mark as HEVC. Defaults to HEVC clips over 10 seconds.")
    p_patch.add_argument("--reference", default=None, help="Original BD backup. When supplied, source CLPI files are restored, patched to HEVC, and their CPI packet maps are scaled to the output streams.")
    p_patch.add_argument("--refresh-cpi", action="store_true", default=False, help="Experimental: splice tsMuxer-generated CPI blocks into CLPI files. Normally leave this off.")
    p_patch.add_argument("--no-refresh-cpi", action="store_false", dest="refresh_cpi", help=argparse.SUPPRESS)
    p_patch.add_argument("--verbose", action="store_true")
    p_patch.set_defaults(func=cmd_patch_navigation)

    p_remux = command_parser(sub, "remux-replacements", help="Remux existing HEVC replacement clips with the current converter M2TS authoring rules.", description="Rebuild replacement M2TS files without reencoding their existing HEVC video.", examples="""
  py bd2hevc.py remux-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py remux-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 00002
""")
    p_remux.add_argument("source", help="Original BD backup folder.")
    p_remux.add_argument("output", help="Converted full-disc output folder.")
    p_remux.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to remux. Defaults to HEVC clips over 10 seconds.")
    p_remux.add_argument("--verbose", action="store_true")
    p_remux.set_defaults(func=cmd_remux_replacements)

    p_reencode = command_parser(sub, "reencode-replacements", help="Reencode selected replacement clips in an existing full-disc output.", description="Reencode selected clips in an existing converted output, then remux and repatch navigation.", examples="""
  py bd2hevc.py reencode-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001
  py bd2hevc.py reencode-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 --bitrate-mode compact-cq --compact-cq-value 20
""")
    p_reencode.add_argument("source", help="Original BD backup folder.")
    p_reencode.add_argument("output", help="Converted full-disc output folder.")
    p_reencode.add_argument("--clips", nargs="+", required=True, help="Clip filenames or ids to reencode.")
    p_reencode.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_reencode)
    add_bitrate_args(p_reencode)
    p_reencode.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_reencode.add_argument("--verbose", action="store_true")
    p_reencode.set_defaults(func=cmd_reencode_replacements)

    p_repair = command_parser(sub, "repair-output", help="Automatically repair an existing converted full-disc output.", description="Inspect and repair an existing converted backup using the current replacement and navigation rules.", examples="""
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --dry-run
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001
""")
    p_repair.add_argument("source", help="Original BD backup folder.")
    p_repair.add_argument("output", help="Converted full-disc output folder.")
    p_repair.add_argument("--clips", nargs="*", default=None, help="Optional clip filenames or ids to force-reencode. Defaults to wrong-bit-depth replacements.")
    p_repair.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="Desired HEVC output bit depth.")
    add_encoder_args(p_repair)
    add_bitrate_args(p_repair)
    p_repair.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each repaired output clip during validation. Use 0 to skip.")
    p_repair.add_argument("--dry-run", action="store_true")
    p_repair.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    p_repair.add_argument("--verbose", action="store_true")
    p_repair.set_defaults(func=cmd_repair_output)

    p_vlc_patch = command_parser(sub, "patch-vlc-compat", help="Apply modular VLC/libbluray compatibility fixes to an existing output.", description="Apply optional BD-J compatibility patches to an already converted backup.", examples="""
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --vlc-fix topmenu-mark-zero-on-return
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --vlc-compat off
""")
    p_vlc_patch.add_argument("target", help="BD/UHD-BD backup folder.")
    add_vlc_compatibility_args(p_vlc_patch)
    p_vlc_patch.add_argument("--json", action="store_true", help="Print the full JSON report.")
    p_vlc_patch.set_defaults(func=cmd_patch_vlc_compat)

    p_vlc = command_parser(sub, "vlc-smoke", help="Headless VLC/libbluray startup smoke test; does not open a visible video window.", description="Run a short VLC startup test against a BD/UHD-BD backup without opening a visible VLC window.", examples="""
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --seconds 45 --video-plane
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --d3d11
""")
    p_vlc.add_argument("target", help="BD/UHD-BD backup folder.")
    p_vlc.add_argument("--seconds", type=float, default=20.0, help="How long VLC should run before exiting.")
    p_vlc.add_argument("--log", default=None, help="VLC log path. Defaults to reports/<disc>.vlc_headless_smoke.log.")
    p_vlc.add_argument("--video-plane", action="store_true", help="Use VLC dummy video output instead of --no-video, exercising video/subpicture paths without opening a visible window.")
    p_vlc.add_argument("--d3d11", action="store_true", help="Force VLC's D3D11VA decoder path and fail on known D3D11 video-freeze warnings.")
    p_vlc.add_argument("--allow-resume", action="store_true", help="Allow VLC to resume remembered playback state instead of forcing menu startup.")
    p_vlc.add_argument("--verbose", action="store_true")
    p_vlc.set_defaults(func=cmd_vlc_smoke)

    p_progress = command_parser(sub, "progress", help="Show a progress bar for a running full-disc conversion.", description="Low-level progress command used by older workflows. For background jobs, prefer 'status --watch'.", examples="""
  py bd2hevc.py progress "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --plan reports\\jobs\\job.plan.json
  py bd2hevc.py progress "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --plan reports\\jobs\\job.plan.json --log reports\\jobs\\job.log --watch
""")
    p_progress.add_argument("target", help="Output BD folder being written.")
    p_progress.add_argument("--plan", required=True, help="Dry-run JSON produced before the matching conversion.")
    p_progress.add_argument("--log", default=None, help="Optional conversion log for current-clip progress.")
    p_progress.add_argument("--width", type=int, default=32)
    p_progress.add_argument("--watch", nargs="?", const=1.0, type=float, default=0, help="Refresh every N seconds until stopped. Defaults to 1 second when no interval is supplied.")
    p_progress.set_defaults(func=cmd_progress)
    return parser


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
