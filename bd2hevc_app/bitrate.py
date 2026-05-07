"""Duration parsing and HEVC bitrate selection."""

from __future__ import annotations

import argparse
import math
from typing import Any

from .config import ANIME_CQ_PRESET, ANIME_CQ_VALUE, BITRATE_MODE_ALIASES, BITRATE_MODES, DEFAULT_ANIME_CQ_MIN_DURATION
from .tools import ToolError


def parse_timecode(value: str | None) -> float | None:
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return None


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00:00"
    seconds_i = max(0, int(round(float(seconds))))
    hours, rem = divmod(seconds_i, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else None
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def bits_to_k(value: int | float | None) -> str:
    if not value:
        return "0k"
    return f"{max(1, int(round(value / 1000)))}k"


def parse_bitrate_arg(value: str) -> int:
    text = value.strip().lower()
    multiplier = 1
    if text.endswith("kbps"):
        multiplier = 1000
        text = text[:-4]
    elif text.endswith("k"):
        multiplier = 1000
        text = text[:-1]
    elif text.endswith("mbps"):
        multiplier = 1_000_000
        text = text[:-4]
    elif text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        parsed = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid bitrate: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Bitrate must be greater than zero")
    return int(parsed * multiplier)


def parse_duration_arg(value: str) -> float:
    text = value.strip().lower()
    if ":" in text:
        parsed = parse_timecode(text)
        if parsed is None or parsed <= 0:
            raise argparse.ArgumentTypeError(f"Invalid duration: {value}")
        return parsed
    multiplier = 1.0
    if text.endswith("seconds"):
        text = text[:-7].strip()
    elif text.endswith("second"):
        text = text[:-6].strip()
    elif text.endswith("secs"):
        text = text[:-4].strip()
    elif text.endswith("sec"):
        text = text[:-3].strip()
    elif text.endswith("s"):
        text = text[:-1].strip()
    elif text.endswith("minutes"):
        multiplier = 60.0
        text = text[:-7].strip()
    elif text.endswith("minute"):
        multiplier = 60.0
        text = text[:-6].strip()
    elif text.endswith("mins"):
        multiplier = 60.0
        text = text[:-4].strip()
    elif text.endswith("min"):
        multiplier = 60.0
        text = text[:-3].strip()
    elif text.endswith("m"):
        multiplier = 60.0
        text = text[:-1].strip()
    elif text.endswith("hours"):
        multiplier = 3600.0
        text = text[:-5].strip()
    elif text.endswith("hour"):
        multiplier = 3600.0
        text = text[:-4].strip()
    elif text.endswith("hrs"):
        multiplier = 3600.0
        text = text[:-3].strip()
    elif text.endswith("hr"):
        multiplier = 3600.0
        text = text[:-2].strip()
    elif text.endswith("h"):
        multiplier = 3600.0
        text = text[:-1].strip()
    try:
        parsed = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid duration: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Duration must be greater than zero")
    return parsed * multiplier


def bitrate_options_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": normalize_bitrate_mode(getattr(args, "bitrate_mode", "balanced")),
        "factor_override": getattr(args, "hevc_bitrate_factor", None),
        "min_bps": getattr(args, "min_video_bitrate", 2_000_000),
        "max_bps": getattr(args, "max_video_bitrate", 80_000_000),
        "maxrate_multiplier": getattr(args, "maxrate_multiplier", 1.55),
        "bufsize_multiplier": getattr(args, "bufsize_multiplier", 2.0),
        "anime_cq_min_duration": getattr(args, "anime_cq_min_duration", DEFAULT_ANIME_CQ_MIN_DURATION),
    }


def normalize_bitrate_mode(value: str | None) -> str:
    mode = value or "balanced"
    return BITRATE_MODE_ALIASES.get(mode, mode)


def fps_to_rational(value: float | None) -> str:
    if not value:
        return "24000/1001"
    common = [
        (23.976, "24000/1001"),
        (24.0, "24/1"),
        (25.0, "25/1"),
        (29.97, "30000/1001"),
        (50.0, "50/1"),
        (59.94, "60000/1001"),
    ]
    for fps, rational in common:
        if abs(value - fps) < 0.02:
            return rational
    return f"{max(1, int(round(value * 1000)))}/1000"


def mbps(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / 1_000_000, 3)


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def equivalent_hevc_bitrate(
    *,
    video_bps: int | None,
    width: int | None,
    height: int | None,
    fps: float | None,
    duration_seconds: float | None = None,
    source_codec: str | None = None,
    mode: str = "balanced",
    factor_override: float | None = None,
    min_bps: int = 2_000_000,
    max_bps: int = 80_000_000,
    maxrate_multiplier: float = 1.55,
    bufsize_multiplier: float = 2.0,
    anime_cq_min_duration: float = DEFAULT_ANIME_CQ_MIN_DURATION,
) -> dict[str, Any]:
    mode = normalize_bitrate_mode(mode)
    if mode not in BITRATE_MODES:
        raise ToolError(f"Unsupported bitrate mode: {mode}")
    if min_bps <= 0 or max_bps <= 0 or min_bps > max_bps:
        raise ToolError("Invalid bitrate bounds")
    if maxrate_multiplier < 1.0:
        raise ToolError("--maxrate-multiplier must be at least 1.0")
    if bufsize_multiplier < 1.0:
        raise ToolError("--bufsize-multiplier must be at least 1.0")
    if anime_cq_min_duration <= 0:
        raise ToolError("--compact-cq-min-duration must be greater than zero")
    if mode == ANIME_CQ_PRESET and factor_override is None and (duration_seconds or 0) >= anime_cq_min_duration:
        maxrate = int(round_to(min(100_000_000, max_bps), 100_000))
        bufsize = int(round_to(min(160_000_000, maxrate * bufsize_multiplier), 100_000))
        return {
            "target_bps": None,
            "target_mbps": None,
            "maxrate_bps": maxrate,
            "maxrate_mbps": mbps(maxrate),
            "bufsize_bps": bufsize,
            "bufsize_mbps": mbps(bufsize),
            "factor": None,
            "mode": mode,
            "rate_control": "cq",
            "cq": ANIME_CQ_VALUE,
            "anime_cq_min_duration_seconds": anime_cq_min_duration,
            "max_mbps": mbps(max_bps),
            "bufsize_multiplier": bufsize_multiplier,
            "source_codec": (source_codec or "").lower() or None,
            "reason": f"compact-cq preset: CQ {ANIME_CQ_VALUE} for clips at least {format_duration(anime_cq_min_duration)}",
        }
    bitrate_mode = "smaller" if mode == ANIME_CQ_PRESET and factor_override is None else mode
    if not video_bps or not width or not height or not fps:
        return {
            "target_bps": None,
            "maxrate_bps": None,
            "bufsize_bps": None,
            "factor": None,
            "mode": mode,
            "rate_control": "vbr",
            "reason": "missing source video bitrate, dimensions, or frame rate",
        }
    codec = (source_codec or "").lower()
    bpppf = video_bps / (width * height * fps)
    effective_min_bps = min_bps
    if factor_override is not None:
        if factor_override <= 0:
            raise ToolError("--hevc-bitrate-factor must be greater than zero")
        factor = factor_override
        factor_reason = "explicit HEVC/source bitrate factor"
    elif bitrate_mode == "source-ratio":
        factor = 0.60
        factor_reason = "fixed source-ratio HEVC/source bitrate factor"
    else:
        if bpppf < 0.07:
            factor = 0.52
        elif bpppf < 0.12:
            factor = lerp(0.55, 0.60, (bpppf - 0.07) / 0.05)
        elif bpppf < 0.20:
            factor = lerp(0.60, 0.68, (bpppf - 0.12) / 0.08)
        else:
            factor = 0.72
        scale = {"smaller": 0.85, "balanced": 1.0, "transparent": 1.15}[bitrate_mode]
        factor *= scale
        codec_scale = 1.0
        codec_reason = "AVC"
        if codec in {"mpeg2video", "mpeg1video"}:
            codec_scale = 0.55
            codec_reason = "MPEG-2"
            effective_min_bps = min(min_bps, max(300_000, int(video_bps * 0.85)))
        elif codec in {"vc1", "wmv3"}:
            codec_scale = 0.82
            codec_reason = "VC-1"
        factor *= codec_scale
        factor_reason = f"HEVC source-equivalent bitrate curve from {codec_reason} bits-per-pixel-per-frame"
    if mode == ANIME_CQ_PRESET:
        factor_reason += "; compact-cq fallback uses smaller for short reencoded clips"
    target = int(round_to(video_bps * factor, 100_000))
    target = max(effective_min_bps, min(target, max_bps))
    maxrate = int(round_to(min(100_000_000, max(target * maxrate_multiplier, target + 2_000_000)), 100_000))
    bufsize = int(round_to(min(160_000_000, maxrate * bufsize_multiplier), 100_000))
    return {
        "target_bps": target,
        "target_mbps": mbps(target),
        "maxrate_bps": maxrate,
        "maxrate_mbps": mbps(maxrate),
        "bufsize_bps": bufsize,
        "bufsize_mbps": mbps(bufsize),
        "factor": round(factor, 3),
        "mode": mode,
        "rate_control": "vbr",
        "fallback_bitrate_mode": bitrate_mode if bitrate_mode != mode else None,
        "anime_cq_min_duration_seconds": anime_cq_min_duration if mode == ANIME_CQ_PRESET else None,
        "min_mbps": mbps(min_bps),
        "effective_min_mbps": mbps(effective_min_bps),
        "max_mbps": mbps(max_bps),
        "maxrate_multiplier": maxrate_multiplier,
        "bufsize_multiplier": bufsize_multiplier,
        "source_bpppf": round(bpppf, 5),
        "source_codec": codec or None,
        "reason": factor_reason,
    }


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def round_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)
