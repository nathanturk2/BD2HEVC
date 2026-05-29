"""FFmpeg HEVC encoding command construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bitrate import bits_to_k, fps_to_rational, normalize_bitrate_mode, safe_float, safe_int
from .config import (
    ANIME_CQ_PRESET,
    AUDIO_MODES,
    DEFAULT_AUDIO_MODE,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_STEREO_AUDIO_BITRATE,
)
from .tools import ToolError, require_tool, run_cmd


def compact_audio_channels(audio: dict[str, Any]) -> int:
    channels = safe_int(audio.get("channels"))
    return 1 if channels == 1 else 2


def compact_audio_source_streams(clip_info: dict[str, Any]) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for audio in clip_info.get("audio") or []:
        channels = safe_int(audio.get("channels"))
        if channels is None or channels <= 0:
            continue
        streams.append(audio)
    return streams


def ffmpeg_audio_map_spec(audio: dict[str, Any], audio_ordinal: int) -> str:
    stream_index = safe_int(audio.get("index"))
    return f"0:{stream_index}" if stream_index is not None else f"0:a:{audio_ordinal}"


def append_compact_audio_options(
    cmd: list[str],
    clip_info: dict[str, Any],
    *,
    stereo_audio_bitrate: int,
    mono_audio_bitrate: int,
) -> None:
    cmd.extend(["-c:a", "ac3"])
    for index, audio in enumerate(compact_audio_source_streams(clip_info)):
        channels = compact_audio_channels(audio)
        bitrate = mono_audio_bitrate if channels == 1 else stereo_audio_bitrate
        cmd.extend(
            [
                f"-ac:a:{index}",
                str(channels),
                f"-b:a:{index}",
                bits_to_k(bitrate),
                f"-ar:a:{index}",
                "48000",
            ]
        )
        language = audio.get("language")
        if language:
            cmd.extend([f"-metadata:s:a:{index}", f"language={language}"])


def transcode_compact_audio_tracks(
    input_path: Path,
    output_prefix: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    stereo_audio_bitrate: int = DEFAULT_STEREO_AUDIO_BITRATE,
    mono_audio_bitrate: int = DEFAULT_MONO_AUDIO_BITRATE,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    ffmpeg = require_tool(tools, "ffmpeg")
    audio_streams = compact_audio_source_streams(clip_info)
    outputs: list[dict[str, Any]] = []
    if not audio_streams:
        return outputs, []
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-stats_period",
        "1",
        "-probesize",
        "500M",
        "-analyzeduration",
        "500M",
        "-i",
        str(input_path),
    ]
    for index, audio in enumerate(audio_streams):
        channels = compact_audio_channels(audio)
        bitrate = mono_audio_bitrate if channels == 1 else stereo_audio_bitrate
        output_path = output_prefix.with_name(f"{output_prefix.stem}.audio{index:02d}.ac3")
        output_path.unlink(missing_ok=True)
        cmd.extend(
            [
                "-map",
                ffmpeg_audio_map_spec(audio, index),
                "-vn",
                "-sn",
                "-dn",
                "-c:a",
                "ac3",
                "-ac",
                str(channels),
                "-b:a",
                bits_to_k(bitrate),
                "-ar",
                "48000",
                str(output_path),
            ]
        )
        outputs.append(
            {
                "path": str(output_path),
                "language": audio.get("language"),
                "channels": channels,
                "bitrate": bitrate,
                "source_index": audio.get("index"),
            }
        )
    if not dry_run:
        run_cmd(cmd, check=True, capture=False, verbose=verbose)
    return outputs, cmd


def encode_to_hevc_m2ts(
    input_path: Path,
    output_path: Path,
    clip_info: dict[str, Any],
    tools: dict[str, Any],
    *,
    sample_seconds: float | None = None,
    sample_start: float = 0.0,
    video_only: bool = False,
    scale_uhd: bool = False,
    hevc_bit_depth: int = 8,
    encoder: str = "hevc_nvenc",
    audio_mode: str = DEFAULT_AUDIO_MODE,
    stereo_audio_bitrate: int = DEFAULT_STEREO_AUDIO_BITRATE,
    mono_audio_bitrate: int = DEFAULT_MONO_AUDIO_BITRATE,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[str]:
    ffmpeg = require_tool(tools, "ffmpeg")
    if audio_mode not in AUDIO_MODES:
        raise ToolError(f"Unsupported audio mode: {audio_mode}")
    video = clip_info.get("video") or {}
    target = video.get("target_hevc") or {}
    fps = video.get("fps") or 23.976
    keyint = max(1, int(round(fps)))
    sparse_low_delay = bool(video.get("sparse_timestamp_video"))
    rate_control = str(target.get("rate_control") or "vbr").lower()
    cq_value = safe_int(target.get("cq"))
    anime_cq_nvenc = (
        encoder == "hevc_nvenc"
        and rate_control == "cq"
        and normalize_bitrate_mode(str(target.get("mode") or "").lower()) == ANIME_CQ_PRESET
    )
    if rate_control == "cq":
        if cq_value is None:
            raise ToolError(f"Could not determine CQ value for {input_path}")
    elif not target.get("target_bps"):
        raise ToolError(f"Could not estimate target HEVC bitrate for {input_path}")
    maxrate_bps = target.get("maxrate_bps") or 80_000_000
    bufsize_bps = target.get("bufsize_bps") or 160_000_000
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-stats_period",
        "1",
        "-probesize",
        "500M",
        "-analyzeduration",
        "500M",
    ]
    if sample_start > 0:
        cmd.extend(["-ss", str(sample_start)])
    cmd.extend(["-i", str(input_path)])
    if sample_seconds:
        cmd.extend(["-t", str(sample_seconds)])
    cmd.extend(["-map", "0:v:0"])
    if not video_only:
        cmd.extend(["-map", "0:a?"])
        if audio_mode == DEFAULT_AUDIO_MODE:
            cmd.extend(["-map", "0:s?"])
    filters = []
    if scale_uhd:
        filters.append("scale=3840:2160:flags=lanczos")
    if video.get("sparse_timestamp_video"):
        filters.append(f"fps={fps_to_rational(fps)}")
        final_hold = safe_float(video.get("sparse_final_hold_seconds"))
        if final_hold and final_hold > 0:
            filters.append(f"tpad=stop_mode=clone:stop_duration={final_hold:.6f}")
    if filters:
        cmd.extend(["-vf", ",".join(filters)])
    if hevc_bit_depth == 8:
        hevc_profile = "main"
        hevc_pix_fmt = "yuv420p"
    elif hevc_bit_depth == 10:
        hevc_profile = "main10"
        hevc_pix_fmt = "p010le"
    else:
        raise ToolError(f"Unsupported HEVC bit depth: {hevc_bit_depth}")
    if encoder == "hevc_nvenc":
        cmd.extend(
            [
                "-c:v:0",
                "hevc_nvenc",
                "-preset",
                "p5",
                "-tune",
                "hq",
                "-profile:v:0",
                hevc_profile,
                "-level:v:0",
                "153",
                "-tier:v:0",
                "main",
                "-pix_fmt",
                hevc_pix_fmt,
                "-rc",
                "vbr",
            ]
        )
        if rate_control == "cq":
            cmd.extend(["-cq", str(cq_value), "-b:v:0", "0k"])
        else:
            cmd.extend(["-b:v:0", bits_to_k(target["target_bps"])])
        if anime_cq_nvenc:
            # HandBrake-style NVENC CQ is much leaner for anime than combining CQ
            # with BD2HEVC's normal AQ/VBV tuning. FFmpeg's -bluray-compat also
            # materially raises NVENC CQ bitrate on anime, so keep explicit
            # AUD/GOP/metadata controls without that size-heavy shortcut.
            pass
        else:
            cmd.extend(
                [
                    "-maxrate:v:0",
                    bits_to_k(maxrate_bps),
                    "-bufsize:v:0",
                    bits_to_k(bufsize_bps),
                    "-spatial-aq",
                    "1",
                    "-temporal-aq",
                    "1",
                    "-aq-strength",
                    "8",
                ]
            )
        cmd.extend(
            [
                "-rc-lookahead",
                "0" if sparse_low_delay else "32",
                "-bf",
                "0" if sparse_low_delay else "3",
                "-b_ref_mode",
                "0",
                "-g",
                str(keyint),
                "-forced-idr",
                "1",
                "-strict_gop",
                "1",
                "-aud",
                "1",
                "-extra_sei",
                "0",
            ]
        )
        if not anime_cq_nvenc:
            cmd.extend(["-bluray-compat", "1"])
    elif encoder == "hevc_qsv":
        if rate_control == "cq":
            raise ToolError(f"{ANIME_CQ_PRESET} currently supports CQ with hevc_nvenc, hevc_amf, and libx265; use --encoder hevc_nvenc or --bitrate-mode smaller for hevc_qsv.")
        cmd.extend(
            [
                "-c:v:0",
                "hevc_qsv",
                "-profile:v:0",
                hevc_profile,
                "-pix_fmt",
                "p010le" if hevc_bit_depth == 10 else "nv12",
                "-b:v:0",
                bits_to_k(target["target_bps"]),
                "-maxrate:v:0",
                bits_to_k(maxrate_bps),
                "-bufsize:v:0",
                bits_to_k(bufsize_bps),
                "-g",
                str(keyint),
                "-bf",
                "0" if sparse_low_delay else "3",
            ]
        )
    elif encoder == "hevc_amf":
        cmd.extend(
            [
                "-c:v:0",
                "hevc_amf",
                "-quality",
                "quality",
                "-profile:v:0",
                hevc_profile,
                "-pix_fmt",
                hevc_pix_fmt,
            ]
        )
        if rate_control == "cq":
            cmd.extend(["-rc", "qvbr", "-qvbr_quality_level", str(cq_value)])
        else:
            cmd.extend(["-rc", "vbr_peak", "-b:v:0", bits_to_k(target["target_bps"])])
        cmd.extend(
            [
                "-maxrate:v:0",
                bits_to_k(maxrate_bps),
                "-bufsize:v:0",
                bits_to_k(bufsize_bps),
                "-g",
                str(keyint),
                "-bf",
                "0" if sparse_low_delay else "3",
            ]
        )
    elif encoder == "libx265":
        x265_params = [
            f"keyint={keyint}",
            f"min-keyint={keyint}",
            "open-gop=0",
            "aud=1",
            "hrd=1",
            "repeat-headers=1",
            f"bframes={0 if sparse_low_delay else 3}",
        ]
        if rate_control == "cq":
            x265_params.extend(
                [
                    f"crf={cq_value}",
                    f"vbv-maxrate={max(1, int(round(maxrate_bps / 1000)))}",
                    f"vbv-bufsize={max(1, int(round(bufsize_bps / 1000)))}",
                ]
            )
        cmd.extend(
            [
                "-c:v:0",
                "libx265",
                "-preset",
                "medium",
                "-profile:v:0",
                hevc_profile,
                "-pix_fmt",
                "yuv420p10le" if hevc_bit_depth == 10 else "yuv420p",
                "-x265-params",
                ":".join(x265_params),
            ]
        )
        if rate_control != "cq":
            cmd.extend(
                [
                    "-b:v:0",
                    bits_to_k(target["target_bps"]),
                    "-maxrate:v:0",
                    bits_to_k(maxrate_bps),
                    "-bufsize:v:0",
                    bits_to_k(bufsize_bps),
                ]
            )
    else:
        raise ToolError(f"Unsupported HEVC encoder: {encoder}")
    cmd.extend(["-bsf:v:0", f"hevc_metadata=aud=insert:tick_rate={fps_to_rational(fps)}:num_ticks_poc_diff_one=1"])
    if video_only:
        cmd.extend(["-f", "hevc", str(output_path)])
    else:
        if audio_mode == "compact-stereo":
            append_compact_audio_options(
                cmd,
                clip_info,
                stereo_audio_bitrate=stereo_audio_bitrate,
                mono_audio_bitrate=mono_audio_bitrate,
            )
        else:
            cmd.extend(["-c:a", "copy", "-c:s", "copy"])
        cmd.extend(["-mpegts_m2ts_mode", "1", "-f", "mpegts", str(output_path)])
    if dry_run:
        return cmd
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(cmd, check=True, capture=False, verbose=verbose)
    return cmd
