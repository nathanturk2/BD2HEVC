"""FFmpeg HEVC encoding command construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bitrate import bits_to_k, fps_to_rational, normalize_bitrate_mode, safe_float, safe_int
from .config import ANIME_CQ_PRESET
from .tools import ToolError, require_tool, run_cmd


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
    dry_run: bool = False,
    verbose: bool = False,
) -> list[str]:
    ffmpeg = require_tool(tools, "ffmpeg")
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
        cmd.extend(["-map", "0:a?", "-map", "0:s?"])
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
        cmd.extend(["-c:a", "copy", "-c:s", "copy", "-mpegts_m2ts_mode", "1", str(output_path)])
    if dry_run:
        return cmd
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(cmd, check=True, capture=False, verbose=verbose)
    return cmd
