#!/usr/bin/env python3
"""
BD2HEVC full-disc Blu-ray HEVC conversion.

This tool is built for local, unencrypted BDMV backups. It uses FFprobe/FFmpeg
for stream inspection and NVENC HEVC encoding, tsMuxeR for Blu-ray M2TS
authoring, and optionally MakeMKV/VLC for validation.
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
    parse_bitrate_arg,
    parse_duration_arg,
    parse_rate,
    parse_timecode,
    safe_float,
    safe_int,
)
from .config import (
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
    MPEG2_SOURCE_CODECS,
    ROOT,
    SECONDS_REENCODE_THRESHOLD,
    SPARSE_TIMING_ALWAYS_COUNT_MAX_DURATION,
    SPARSE_TIMING_FRAME_COUNT_MAX_DURATION,
    SPARSE_TIMING_MIN_GAP_SECONDS,
    SPARSE_TIMING_MIN_RATIO,
    VERSION,
)
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


def apply_main_title_cq_override(clips: list[dict[str, Any]], cq_value: int | None) -> dict[str, Any] | None:
    if cq_value is None:
        return None
    if cq_value < 0 or cq_value > 51:
        raise ToolError("--main-title-cq must be between 0 and 51")
    candidates = [
        clip
        for clip in clips
        if clip.get("action") == "reencode"
        and (((clip.get("video") or {}).get("target_hevc") or {}).get("rate_control") == "cq")
    ]
    main_clip = max(candidates, key=lambda item: float(item.get("duration") or 0), default=None)
    if not main_clip:
        return None
    target = ((main_clip.get("video") or {}).get("target_hevc") or {})
    previous = safe_int(target.get("cq"))
    target["cq"] = cq_value
    target["compact_cq_value"] = cq_value
    target["main_title_cq_override"] = True
    reason = target.get("reason")
    target["reason"] = f"{reason}; main title CQ override {previous} -> {cq_value}" if reason else f"main title CQ override {previous} -> {cq_value}"
    return {
        "file": main_clip.get("file"),
        "duration": main_clip.get("duration"),
        "previous_cq": previous,
        "cq": cq_value,
    }

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

    bitrate_options = bitrate_options_from_args(args)
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
    clips: list[dict[str, Any]],
    *,
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
        "audio": {
            "mode": audio_mode_from_args(args),
            "stereo_bitrate": stereo_audio_bitrate_from_args(args),
            "mono_bitrate": mono_audio_bitrate_from_args(args),
        },
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


def finalize_clone_clip_context(ctx: dict[str, Any], tools: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    compact_audio = audio_mode_from_args(args) == "compact-stereo"
    if compact_audio:
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


def convert_clone_streams(args: argparse.Namespace, tools: dict[str, Any]) -> dict[str, Any]:
    if args.uhd_scale or args.skip_audio or args.skip_subtitles:
        raise ToolError("--uhd-scale, --skip-audio, and --skip-subtitles are only supported by movie-only mode")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve() if args.output else default_output_for(source, "clone-streams")
    bitrate_options = bitrate_options_from_args(args)
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
    main_title_cq_override = apply_main_title_cq_override(scan.get("clips", []), getattr(args, "main_title_cq", None))
    clips = [c for c in scan.get("clips", []) if c.get("action") == "reencode"]
    progress_plan_path = path_or_none(getattr(args, "progress_plan", None))
    plan_payload = clone_streams_plan_payload(
        source,
        output,
        args,
        bitrate_options,
        main_title_cq_override,
        clips,
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
    if encode_ahead:
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
        "audio": {
            "mode": audio_mode_from_args(args),
            "stereo_bitrate": stereo_audio_bitrate_from_args(args),
            "mono_bitrate": mono_audio_bitrate_from_args(args),
        },
        "vlc_compatibility": getattr(args, "vlc_compat", DEFAULT_VLC_COMPATIBILITY_MODE),
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
        report = scan_disc(
            root,
            tools,
            accurate_video_bitrate=args.accurate_video_bitrate,
            bitrate_options=bitrate_options_from_args(args),
            use_makemkv=use_makemkv_from_args(args),
            verbose=args.verbose,
        )
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


def cmd_convert(args: argparse.Namespace) -> int:
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
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
            bitrate_options=bitrate_options_from_args(args),
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
            bitrate_options=bitrate_options_from_args(args),
            verbose=args.verbose,
        )
        if output_clpi.exists():
            backup_clpi.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_clpi, backup_clpi)
            report["backup_clpi"] = str(backup_clpi)
        reports.append(report)
    navigation_patch = patch_navigation_for_hevc(output_root, clip_names, tools=tools, source_root=source_root) if clip_names else None
    payload = {
        "source": str(source_root),
        "output": str(output_root),
        "hevc_bit_depth": args.hevc_bit_depth,
        "encoder": selected_hevc_encoder(args),
        "selected_count": len(selected),
        "selected": selected,
        "reports": reports,
        "navigation_patch": navigation_patch,
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
    tools = discover_tools()
    for key in ("ffmpeg", "ffprobe", "tsmuxer"):
        require_tool(tools, key)
    require_hevc_encoder(tools, selected_hevc_encoder(args))
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


def add_bitrate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bitrate-mode", choices=BITRATE_MODES, default="balanced", help="HEVC bitrate preset. balanced is the tested default; smaller saves more space; transparent spends more bitrate; source-ratio uses a fixed multiplier; compact-cq uses configurable CQ for reencoded clips. episode-compact and anime-cq18 are accepted as legacy aliases.")
    parser.add_argument("--bitrate-preset-file", default=None, help="Load bitrate settings from a JSON preset file. Non-default CLI options override preset fields.")
    parser.add_argument("--hevc-bitrate-factor", type=float, default=None, help="Override bitrate mode with a fixed HEVC/source video bitrate multiplier, e.g. 0.62.")
    parser.add_argument("--min-video-bitrate", type=parse_bitrate_arg, default=2_000_000, help="Minimum target video bitrate. Accepts values like 2000k or 2M.")
    parser.add_argument("--max-video-bitrate", type=parse_bitrate_arg, default=80_000_000, help="Maximum target video bitrate. Accepts values like 80M.")
    parser.add_argument("--maxrate-multiplier", type=float, default=1.55, help="VBV maxrate multiplier relative to target bitrate.")
    parser.add_argument("--bufsize-multiplier", type=float, default=2.0, help="VBV buffer multiplier relative to maxrate.")
    parser.add_argument("--compact-cq-value", "--anime-cq-value", dest="compact_cq_value", type=int, default=ANIME_CQ_VALUE, help="CQ value for reencoded clips when --bitrate-mode compact-cq is used. Lower is larger/higher quality; default 18.")
    parser.add_argument("--compact-cq-min-duration", "--episode-compact-min-duration", "--anime-cq-min-duration", dest="anime_cq_min_duration", type=parse_duration_arg, default=DEFAULT_ANIME_CQ_MIN_DURATION, help="Minimum clip duration for --bitrate-mode compact-cq to use CQ. Defaults to the 10-second reencode threshold. Raise it if only episode/movie-length clips should use CQ. Accepts values like 15m or 00:15:00. Shorter reencoded clips use smaller. --episode-compact-min-duration and --anime-cq-min-duration are accepted as legacy aliases.")
    parser.add_argument("--main-title-cq", type=int, default=None, help="Override compact-cq for the longest reencoded clip. Lower is larger/higher quality; useful for CQ20 extras with a CQ18 main movie.")


def add_encoder_args(parser: argparse.ArgumentParser, *, include_encode_ahead: bool = False) -> None:
    parser.add_argument("--encoder", choices=HEVC_ENCODERS, default="hevc_nvenc", help="HEVC encoder to use. Hardware encoders can overlap next-clip encoding with current-clip muxing; libx265 stays serial.")
    if include_encode_ahead:
        parser.add_argument("--no-encode-ahead", action="store_true", help="Disable hardware encode-ahead pipelining and run encode/mux serially.")
        parser.add_argument("--encode-ahead-depth", type=int, default=3, help="Maximum completed HEVC temp clips allowed to wait for the single muxer. Hardware encoders only; default 3.")


def add_audio_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio-mode", choices=AUDIO_MODES, default=DEFAULT_AUDIO_MODE, help="Audio handling for reencoded clips. passthrough keeps source audio; compact-stereo converts every audio track to AC-3 stereo, or mono when the source stream is mono.")
    parser.add_argument("--stereo-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_STEREO_AUDIO_BITRATE, help="Bitrate for compact-stereo two-channel AC-3 audio. Default 256k.")
    parser.add_argument("--mono-audio-bitrate", type=parse_bitrate_arg, default=DEFAULT_MONO_AUDIO_BITRATE, help="Bitrate for compact-stereo mono AC-3 audio. Default 128k.")


def add_makemkv_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--makemkv", action="store_true", help="Use MakeMKV title scanning/validation for folder backups. Disabled by default to avoid probing physical optical drives.")
    parser.add_argument("--no-makemkv", action="store_true", help="Skip MakeMKV title scanning/validation. Conversion still uses FFprobe/FFmpeg/tsMuxer.")
    parser.add_argument("--require-makemkv", action="store_true", help="Fail if MakeMKV validation is unavailable or fails.")


def add_vlc_compatibility_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vlc-compat", choices=["auto", "off"], default=DEFAULT_VLC_COMPATIBILITY_MODE, help="Apply optional VLC/libbluray compatibility fixes. Use off for the closest possible copy of the source BD-J.")
    parser.add_argument("--vlc-fix", action="append", choices=sorted(KNOWN_VLC_COMPATIBILITY_FIXES), help="Apply a specific built-in VLC compatibility fix. Can be repeated. Overrides the built-in auto fix set.")
    parser.add_argument("--compat-patch-file", action="append", help="JSON file with custom JAR/class compatibility patches.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BD2HEVC: convert local Blu-ray backups to HEVC while preserving menus, extras, audio, and subtitles.")
    parser.add_argument("--version", action="version", version=f"BD2HEVC {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tools = sub.add_parser("tools", help="Show discovered external tools and NVENC support.")
    p_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools.set_defaults(func=cmd_tools)

    p_scan = sub.add_parser("scan", help="Scan one or more BDMV backups with MakeMKV and FFprobe.")
    p_scan.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_scan.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p_scan.add_argument("--accurate-video-bitrate", action="store_true", help="Sum video packet sizes for bitrate. Slower, but best for encode planning.")
    add_bitrate_args(p_scan)
    add_makemkv_args(p_scan)
    p_scan.add_argument("--verbose", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_convert = sub.add_parser("convert", help="Convert a BD backup.")
    add_convert_args(p_convert)
    p_convert.set_defaults(func=cmd_convert)

    p_auto = sub.add_parser("auto", help="Faithful full-disc conversion. Only the source backup path is required.")
    p_auto.add_argument("source", help="Source BD backup folder.")
    p_auto.add_argument("output", nargs="?", default=None, help="Output folder. Defaults to <source>_FULL_DISC_HEVC.")
    p_auto.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_auto.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_auto.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth. 8 preserves 8-bit BD sources and is VLC-friendly; use 10 for explicit Main10 output.")
    add_encoder_args(p_auto, include_encode_ahead=True)
    add_bitrate_args(p_auto)
    add_audio_args(p_auto)
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

    p_start = sub.add_parser("start", help="Start a full-disc conversion in the background.")
    p_start.add_argument("source", help="Source BD backup folder.")
    p_start.add_argument("output", nargs="?", default=None, help="Output folder. Defaults next to the source.")
    p_start.add_argument("--name", default=None, help="Friendly job id. Defaults to timestamp plus source folder name.")
    p_start.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_start.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_start.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_start, include_encode_ahead=True)
    add_bitrate_args(p_start)
    add_audio_args(p_start)
    p_start.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_start.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    add_makemkv_args(p_start)
    p_start.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_start.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(p_start)
    p_start.add_argument("--verbose", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_queue = sub.add_parser("queue", help="Queue multiple full-disc conversions that run one at a time.")
    p_queue.add_argument("sources", nargs="+", help="Source BD backup folders or parent folders containing BDMV backups.")
    p_queue.add_argument("--output-dir", default=None, help="Put each converted output in this folder using '<Title> (BD) (UHD converted)' names.")
    p_queue.add_argument("--name-prefix", default=None, help="Prefix for generated job ids. Defaults to the current timestamp.")
    p_queue.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_queue.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_queue.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_queue, include_encode_ahead=True)
    add_bitrate_args(p_queue)
    add_audio_args(p_queue)
    p_queue.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_queue.add_argument("--force", action="store_true", help="Replace existing output folders.")
    add_makemkv_args(p_queue)
    p_queue.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_queue.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    add_vlc_compatibility_args(p_queue)
    p_queue.add_argument("--verbose", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_status = sub.add_parser("status", help="Show progress for a background conversion.")
    p_status.add_argument("job", nargs="?", default=None, help="Job id, output folder, or source folder. Defaults to the newest job.")
    p_status.add_argument("--watch", nargs="?", const=1.0, type=float, default=0, help="Refresh every N seconds. Defaults to 1 second when no interval is supplied.")
    p_status.add_argument("--width", type=int, default=32)
    p_status.set_defaults(func=cmd_status)

    p_jobs = sub.add_parser("jobs", help="List recent background conversions.")
    p_jobs.add_argument("--limit", type=int, default=10)
    p_jobs.set_defaults(func=cmd_jobs)

    p_pause = sub.add_parser("pause-queue", help="Pause the background queue after the current running job.")
    p_pause.add_argument("--reason", default=None, help="Optional note saved with the pause marker.")
    p_pause.set_defaults(func=cmd_pause_queue)

    p_resume = sub.add_parser("resume-queue", help="Resume a paused background queue.")
    p_resume.set_defaults(func=cmd_resume_queue)

    p_cancel = sub.add_parser("cancel", help="Cancel a queued job. Use --kill to stop a running job.")
    p_cancel.add_argument("job", help="Job id, output folder, or source folder.")
    p_cancel.add_argument("--kill", action="store_true", help="Stop a running conversion process tree.")
    p_cancel.set_defaults(func=cmd_cancel_job)

    p_remove = sub.add_parser("remove", help="Remove a job from the queue/status list without deleting converted output.")
    p_remove.add_argument("job", help="Job id, output folder, or source folder.")
    p_remove.add_argument("--kill", action="store_true", help="Allow removal of a running job by stopping it first.")
    p_remove.set_defaults(func=cmd_remove_job)

    p_validate = sub.add_parser("validate", help="Validate an output clip or BDMV folder.")
    p_validate.add_argument("target")
    p_validate.add_argument("--source-backup", action="store_true", help="Validate a source BD backup without requiring HEVC output clips.")
    p_validate.add_argument("--reference", default=None, help="Original BD backup folder to compare matching stream audio and timestamps against.")
    p_validate.add_argument("--decode-sample", type=float, default=None, help="Decode the first N seconds of video to null.")
    p_validate.add_argument("--report", default=None, help="Write the full JSON validation report to this path.")
    p_validate.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    add_makemkv_args(p_validate)
    p_validate.add_argument("--verbose", action="store_true")
    p_validate.set_defaults(func=cmd_validate)

    p_playlist = sub.add_parser("playlist-probe", help="Probe a Blu-ray playlist through libbluray/FFprobe and fail on stale CLPI packet maps.")
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

    p_metadata = sub.add_parser("patch-disc-metadata", help="Create fallback BD disc-library metadata when a backup is missing it.")
    p_metadata.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_metadata.add_argument("--title", default=None, help="Use this title for every patched disc. Defaults to a cleaned folder name.")
    p_metadata.add_argument("--force", action="store_true", help="Overwrite existing bdmt_*.xml metadata.")
    p_metadata.set_defaults(func=cmd_patch_disc_metadata)

    p_patch = sub.add_parser("patch-navigation", help="Patch full-disc CLPI/MPLS descriptors for HEVC replacement clips.")
    p_patch.add_argument("target")
    p_patch.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to mark as HEVC. Defaults to HEVC clips over 10 seconds.")
    p_patch.add_argument("--reference", default=None, help="Original BD backup. When supplied, source CLPI files are restored, patched to HEVC, and their CPI packet maps are scaled to the output streams.")
    p_patch.add_argument("--refresh-cpi", action="store_true", default=False, help="Experimental: splice tsMuxer-generated CPI blocks into CLPI files. Normally leave this off.")
    p_patch.add_argument("--no-refresh-cpi", action="store_false", dest="refresh_cpi", help=argparse.SUPPRESS)
    p_patch.add_argument("--verbose", action="store_true")
    p_patch.set_defaults(func=cmd_patch_navigation)

    p_remux = sub.add_parser("remux-replacements", help="Remux existing HEVC replacement clips with the current converter M2TS authoring rules.")
    p_remux.add_argument("source", help="Original BD backup folder.")
    p_remux.add_argument("output", help="Converted full-disc output folder.")
    p_remux.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to remux. Defaults to HEVC clips over 10 seconds.")
    p_remux.add_argument("--verbose", action="store_true")
    p_remux.set_defaults(func=cmd_remux_replacements)

    p_reencode = sub.add_parser("reencode-replacements", help="Reencode selected replacement clips in an existing full-disc output.")
    p_reencode.add_argument("source", help="Original BD backup folder.")
    p_reencode.add_argument("output", help="Converted full-disc output folder.")
    p_reencode.add_argument("--clips", nargs="+", required=True, help="Clip filenames or ids to reencode.")
    p_reencode.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    add_encoder_args(p_reencode)
    add_bitrate_args(p_reencode)
    p_reencode.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_reencode.add_argument("--verbose", action="store_true")
    p_reencode.set_defaults(func=cmd_reencode_replacements)

    p_repair = sub.add_parser("repair-output", help="Automatically repair an existing converted full-disc output.")
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

    p_vlc_patch = sub.add_parser("patch-vlc-compat", help="Apply modular VLC/libbluray compatibility fixes to an existing output.")
    p_vlc_patch.add_argument("target", help="BD/UHD-BD backup folder.")
    add_vlc_compatibility_args(p_vlc_patch)
    p_vlc_patch.add_argument("--json", action="store_true", help="Print the full JSON report.")
    p_vlc_patch.set_defaults(func=cmd_patch_vlc_compat)

    p_vlc = sub.add_parser("vlc-smoke", help="Headless VLC/libbluray startup smoke test; does not open a visible video window.")
    p_vlc.add_argument("target", help="BD/UHD-BD backup folder.")
    p_vlc.add_argument("--seconds", type=float, default=20.0, help="How long VLC should run before exiting.")
    p_vlc.add_argument("--log", default=None, help="VLC log path. Defaults to reports/<disc>.vlc_headless_smoke.log.")
    p_vlc.add_argument("--video-plane", action="store_true", help="Use VLC dummy video output instead of --no-video, exercising video/subpicture paths without opening a visible window.")
    p_vlc.add_argument("--d3d11", action="store_true", help="Force VLC's D3D11VA decoder path and fail on known D3D11 video-freeze warnings.")
    p_vlc.add_argument("--allow-resume", action="store_true", help="Allow VLC to resume remembered playback state instead of forcing menu startup.")
    p_vlc.add_argument("--verbose", action="store_true")
    p_vlc.set_defaults(func=cmd_vlc_smoke)

    p_progress = sub.add_parser("progress", help="Show a progress bar for a running full-disc conversion.")
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
    if getattr(args, "decode_sample", None) == 0:
        args.decode_sample = None
    try:
        return args.func(args)
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
