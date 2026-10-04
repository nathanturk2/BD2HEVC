"""Conversion workflow: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def _convert_clone_streams_in_place(args: argparse.Namespace, tools: dict[str, Any], *, services) -> dict[str, Any]:
    if args.uhd_scale or args.skip_audio or args.skip_subtitles:
        raise services.ToolError("--uhd-scale, --skip-audio, and --skip-subtitles are only supported by movie-only mode")
    services.validate_cq_override_args(args)
    source = services.Path(args.source).resolve()
    output = services.Path(args.output).resolve() if args.output else services.default_output_for(source, "clone-streams")
    bitrate_options = services.bitrate_options_for_args(args)
    if not args.dry_run:
        services.make_output_available(output, source, force=args.force)
    scan = services.scan_clone_source_for_plan(source, tools, args, bitrate_options)
    inventory = scan.get("clips") or []
    failed = [clip.get("file", "unknown") for clip in inventory if clip.get("ok") is False]
    if scan.get("error") or not inventory or failed:
        raise services.ToolError(f"Source scan failed or contains no clips: {scan.get('error') or ', '.join(failed) or source}")
    services.remember_original_clip_actions(scan.get("clips", []))
    quality_overrides = services.apply_quality_overrides(
        scan.get("clips", []), bitrate_options, args, scan.get("main_feature")
    )
    main_title_cq_override = (quality_overrides or {}).get("main_title_cq")
    top_n_cq_override = (quality_overrides or {}).get("top_n_cq")
    copy_clip_overrides = services.apply_clip_copy_overrides(scan.get("clips", []), getattr(args, "copy_clips", None))
    postprocess = services.apply_deinterlace_plan(scan.get("clips", []), args)
    disc_fit = services.fit_reencoded_clips_to_disc_size(
        source,
        scan.get("clips", []),
        target_size=getattr(args, "target_disc_size", None),
        margin=getattr(args, "target_disc_margin", 0.98),
        audio_mode=services.audio_mode_from_args(args),
    )
    clips = [c for c in scan.get("clips", []) if c.get("action") == "reencode"]
    compact_audio_remux_clips = (
        [c for c in scan.get("clips", []) if services.clip_needs_compact_audio_remux(c)]
        if services.audio_mode_from_args(args) == "compact-stereo"
        else []
    )
    progress_plan_path = services.path_or_none(getattr(args, "progress_plan", None))
    plan_payload = services.clone_streams_plan_payload(
        source,
        output,
        args,
        bitrate_options,
        main_title_cq_override,
        top_n_cq_override,
        clips,
        compact_audio_remux_clips=compact_audio_remux_clips,
        quality_overrides=quality_overrides,
        copy_clip_overrides=copy_clip_overrides,
        postprocess=postprocess,
        target_disc_fit=disc_fit,
        planning=scan.get("planning"),
    )
    if progress_plan_path:
        progress_plan_path.parent.mkdir(parents=True, exist_ok=True)
        services.save_job(progress_plan_path, plan_payload)
    if args.dry_run:
        return plan_payload
    copy_report = services.copy_disc_tree_skipping_reencoded_streams(source, output, {str(clip.get("file")) for clip in clips})
    disc_metadata = services.ensure_disc_library_metadata(output)
    validations = []
    total_seconds = sum(float(clip.get("duration") or 0) for clip in clips)
    done_seconds = 0.0
    progress_enabled = not getattr(args, "no_progress", False)
    contexts = [services.clone_clip_context(source, output, clip) for clip in clips]
    encoder = services.selected_hevc_encoder(args)
    encode_ahead = len(contexts) > 1 and services.encoder_is_hardware(encoder) and not getattr(args, "no_encode_ahead", False)
    compact_audio_pipeline = encode_ahead and services.audio_mode_from_args(args) == "compact-stereo"
    if encode_ahead:
        if compact_audio_pipeline:
            validations, done_seconds = services.run_queued_encode_audio_mux_pipeline(
                contexts,
                tools,
                args,
                total_seconds=total_seconds,
                progress_enabled=progress_enabled,
            )
        else:
            validations, done_seconds = services.run_queued_encode_mux_pipeline(
                contexts,
                tools,
                args,
                total_seconds=total_seconds,
                progress_enabled=progress_enabled,
            )
    else:
        for index, ctx in enumerate(contexts, start=1):
            services.emit_conversion_progress(
                done_seconds,
                total_seconds,
                len(validations),
                len(clips),
                current=ctx["file"],
                stage="encoding",
                enabled=progress_enabled,
            )
            services.encode_clone_clip_context(ctx, tools, args)
            validation = services.finalize_clone_clip_context(ctx, tools, args)
            validations.append(validation)
            done_seconds += float(ctx["clip"].get("duration") or 0)
            services.emit_conversion_progress(
                done_seconds,
                total_seconds,
                index,
                len(clips),
                current=ctx["file"],
                stage="validated",
                enabled=progress_enabled,
            )
    compact_audio_remux_validations: list[dict[str, Any]] = []
    for clip in compact_audio_remux_clips:
        ctx = services.clone_clip_context(source, output, clip)
        services.progress_event("audio-remux-start", ctx["file"])
        compact_audio_remux_validations.append(services.remux_compact_audio_copy_context(ctx, tools, args))
        services.progress_event("audio-remux-done", ctx["file"])
    validations.extend(compact_audio_remux_validations)
    checked = {services.Path(str(item.get("output", ""))).name for item in validations}
    for clip in scan["clips"]:
        name = str(clip["file"])
        if name not in checked:
            validations.append(services.validate_clip(source / "BDMV" / "STREAM" / name,
                output / "BDMV" / "STREAM" / name, tools, require_hevc="never"))
    navigation_patch = None
    services.emit_conversion_progress(
        done_seconds,
        total_seconds,
        len(clips),
        len(clips),
        current=None,
        stage="post-processing",
        enabled=progress_enabled,
    )
    if args.patch_navigation:
        compact_audio_clip_files = (
            [clip["file"] for clip in clips if services.compact_audio_source_streams(clip)]
            + [clip["file"] for clip in compact_audio_remux_clips]
            if services.audio_mode_from_args(args) == "compact-stereo"
            else []
        )
        navigation_patch = services.patch_navigation_for_hevc(
            output,
            [clip["file"] for clip in clips],
            tools=tools,
            source_root=source,
            compact_audio_clip_files=compact_audio_clip_files,
            patch_version_headers=services.patch_version_headers_from_args(args),
        )
    uhd_structure = services.ensure_uhd_backup_structure(output, patch_version_headers=services.patch_version_headers_from_args(args))
    bdj_compatibility_patch = None
    selected_vlc_fixes = services.compatibility_fix_names_from_args(args)
    custom_patch_files = services.custom_compatibility_patch_files_from_args(args)
    if selected_vlc_fixes or custom_patch_files:
        bdj_compatibility_patch = services.patch_known_bdj_compatibility(
            output,
            fixes=selected_vlc_fixes,
            custom_patch_files=custom_patch_files,
        )
    makemkv_validation = services.validate_disc_titles(
        output,
        tools,
        use_makemkv=services.use_makemkv_from_args(args),
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
        "postprocess": postprocess,
        "audio": {
            "mode": services.audio_mode_from_args(args),
            "stereo_bitrate": services.stereo_audio_bitrate_from_args(args),
            "mono_bitrate": services.mono_audio_bitrate_from_args(args),
        },
        "vlc_compatibility": getattr(args, "vlc_compat", services.DEFAULT_VLC_COMPATIBILITY_MODE),
        "target_disc_fit": disc_fit,
        "planning": scan.get("planning"),
        "uhd_profile": services.normalize_uhd_profile(getattr(args, "uhd_profile", "library")),
        "uhd_structure": uhd_structure,
        "vlc_fixes": selected_vlc_fixes,
        "custom_compatibility_patch_files": [str(path) for path in custom_patch_files],
        "encode_ahead": encode_ahead,
        "encode_ahead_depth": getattr(args, "encode_ahead_depth", 3) if encode_ahead else 0,
        "source_inventory": [clip["file"] for clip in scan["clips"]],
        "validation_coverage": {"expected": len(scan["clips"]), "checked": len(validations)},
        "preservation_copy": copy_report,
        "disc_metadata": disc_metadata,
        "reencoded": [c["file"] for c in clips],
        "compact_audio_remuxed": [c["file"] for c in compact_audio_remux_clips],
        "validation": validations,
        "makemkv_validation": makemkv_validation,
    }
    if encode_ahead:
        result["pipeline"] = "video-audio-mux-queue" if compact_audio_pipeline else "encode-mux-queue"
    if navigation_patch is not None:
        result["navigation_patch"] = navigation_patch
    if bdj_compatibility_patch is not None:
        result["bdj_compatibility_patch"] = bdj_compatibility_patch
    services.emit_conversion_progress(
        done_seconds,
        total_seconds,
        len(clips),
        len(clips),
        current=None,
        stage="completed",
        enabled=progress_enabled,
    )
    return result
