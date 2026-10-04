"""Planning workflow: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def scan_clone_source_for_plan(
    source: Path,
    tools: dict[str, Any],
    args: argparse.Namespace,
    bitrate_options: dict[str, Any], *, services) -> dict[str, Any]:
    accurate_requested = not getattr(args, "fast_bitrate", False)
    scan = services.scan_disc(
        source,
        tools,
        accurate_video_bitrate=False,
        depad_video_padding=services.depad_video_padding_from_args(args),
        bitrate_options=bitrate_options,
        use_makemkv=services.use_makemkv_from_args(args),
        verbose=args.verbose,
    )
    feature_selection = services.main_feature_selection(
        source,
        known_clip_ids={services.Path(str(clip.get("file") or "")).stem for clip in scan.get("clips", [])},
    )
    scan["main_feature"] = feature_selection
    bitrate_files = services.source_bitrate_files_for_effective_plan(
        scan.get("clips", []), bitrate_options, args, feature_selection
    ) if accurate_requested else set()
    if bitrate_files:
        services.refine_disc_video_bitrates(
            scan,
            tools,
            bitrate_files,
            depad_video_padding=services.depad_video_padding_from_args(args),
            bitrate_options=bitrate_options,
        )
    scan["planning"] = {
        "strategy": "metadata-then-selective-bitrate",
        "accurate_bitrate_requested": accurate_requested,
        "accurate_bitrate_clips": scan.get("accurate_bitrate_clips", []),
        "accurate_bitrate_skipped_clips": sorted(
            str(clip.get("file") or "")
            for clip in scan.get("clips", [])
            if clip.get("action") == "reencode" and str(clip.get("file") or "") not in bitrate_files
        ),
    }
    return scan


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
    planning_pending: bool = False, services) -> dict[str, Any]:
    return {
        "mode": "clone-streams",
        "warning": "full-disc mode preserves the original menu/extras structure and patches replacement-video navigation metadata",
        "source": str(source),
        "output": str(output),
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": services.selected_hevc_encoder(args),
        "bitrate": bitrate_options,
        "main_title_cq_override": main_title_cq_override,
        "top_n_cq_override": top_n_cq_override,
        "quality_overrides": quality_overrides,
        "copy_clip_overrides": copy_clip_overrides,
        "postprocess": postprocess,
        "audio": {
            "mode": services.audio_mode_from_args(args),
            "stereo_bitrate": services.stereo_audio_bitrate_from_args(args),
            "mono_bitrate": services.mono_audio_bitrate_from_args(args),
        },
        "source_padding": {
            "mode": "subtract_safe_coded_padding" if services.depad_video_padding_from_args(args) else "keep",
            "note": "Library planning subtracts safe AVC/HEVC filler and VC-1 stuffing from accurate source video bitrate. --keep-source-padding and --uhd-profile disc keep the padded-source estimate.",
        },
        "target_disc_fit": target_disc_fit,
        "planning": planning,
        "uhd_profile": services.normalize_uhd_profile(getattr(args, "uhd_profile", "library")),
        "uhd_structure": "always",
        "patch_navigation": args.patch_navigation,
        "bdj_compatibility_patches": bool(getattr(args, "bdj_compatibility_patches", False)),
        "vlc_compatibility": getattr(args, "vlc_compat", services.DEFAULT_VLC_COMPATIBILITY_MODE),
        "vlc_fixes": services.compatibility_fix_names_from_args(args),
        "custom_compatibility_patch_files": [str(path) for path in services.custom_compatibility_patch_files_from_args(args)],
        "encode_ahead": services.encoder_is_hardware(services.selected_hevc_encoder(args)) and not getattr(args, "no_encode_ahead", False),
        "encode_ahead_depth": getattr(args, "encode_ahead_depth", 3),
        "planning_pending": planning_pending,
        "reencode_clips": [services.clip_summary(c) for c in clips],
        "compact_audio_remux_clips": [services.clip_summary(c) for c in (compact_audio_remux_clips or [])],
    }
