"""Duration parsing and HEVC bitrate selection."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from .config import (
    ANIME_CQ_PRESET,
    ANIME_CQ_VALUE,
    BITRATE_MODE_ALIASES,
    BITRATE_MODES,
    DEFAULT_ANIME_CQ_MIN_DURATION,
)
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


def load_bitrate_preset_file(path_value: str | None) -> dict[str, Any]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolError(f"Could not read bitrate preset file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON in bitrate preset file: {path}") from exc
    if not isinstance(data, dict):
        raise ToolError("Bitrate preset file must contain a JSON object")
    return data


def preset_value(preset: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in preset:
            return preset[key]
    return None


def preset_bitrate(value: Any, key: str) -> int:
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ToolError(f"{key} must be greater than zero")
        return int(value)
    if isinstance(value, str):
        try:
            return parse_bitrate_arg(value)
        except argparse.ArgumentTypeError as exc:
            raise ToolError(f"Invalid {key}: {value}") from exc
    raise ToolError(f"{key} must be a bitrate string or number")


def preset_duration(value: Any, key: str) -> float:
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ToolError(f"{key} must be greater than zero")
        return float(value)
    if isinstance(value, str):
        try:
            return parse_duration_arg(value)
        except argparse.ArgumentTypeError as exc:
            raise ToolError(f"Invalid {key}: {value}") from exc
    raise ToolError(f"{key} must be a duration string or number")


def preset_float(value: Any, key: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key} must be a number") from exc
    return parsed


def preset_positive_float(value: Any, key: str) -> float:
    parsed = preset_float(value, key)
    if parsed <= 0:
        raise ToolError(f"{key} must be greater than zero")
    return parsed


def preset_int(value: Any, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{key} must be an integer") from exc
    return parsed


def preset_or_arg(
    args: argparse.Namespace,
    attr: str,
    default: Any,
    preset: dict[str, Any],
    keys: tuple[str, ...],
    converter: Any,
) -> Any:
    arg_value = getattr(args, attr, default)
    raw = preset_value(preset, *keys)
    if raw is not None and arg_value == default:
        return converter(raw, keys[0])
    return arg_value


def normalize_codec_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    compact = text.replace(".", "").replace("-", "").replace("_", "").replace(" ", "")
    if compact in {"h264", "avc", "mpeg4avc", "avc1"}:
        return "h264"
    if compact in {"mpeg2", "mpeg2video", "mpeg1", "mpeg1video", "mpgv"}:
        return "mpeg2video"
    if compact in {"vc1", "wmv3"}:
        return "vc1"
    return compact or text


def parse_codec_factor_pair(value: Any, *, key: str) -> tuple[str, float]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        codec_raw, factor_raw = value
    elif isinstance(value, str):
        if "=" in value:
            codec_raw, factor_raw = value.split("=", 1)
        elif ":" in value:
            codec_raw, factor_raw = value.split(":", 1)
        else:
            raise ToolError(f"{key} entries must use CODEC=FACTOR")
    else:
        raise ToolError(f"{key} entries must use CODEC=FACTOR")
    codec = normalize_codec_key(codec_raw)
    if not codec:
        raise ToolError(f"{key} codec cannot be empty")
    factor = preset_positive_float(factor_raw, f"{key}.{codec}")
    return codec, factor


def codec_factor_overrides_from_value(value: Any, *, key: str) -> dict[str, float]:
    if value is None:
        return {}
    parsed: dict[str, float] = {}
    if isinstance(value, dict):
        for codec_raw, factor_raw in value.items():
            codec = normalize_codec_key(codec_raw)
            if not codec:
                raise ToolError(f"{key} codec cannot be empty")
            parsed[codec] = preset_positive_float(factor_raw, f"{key}.{codec}")
        return parsed
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        for item in items:
            codec, factor = parse_codec_factor_pair(item, key=key)
            parsed[codec] = factor
        return parsed
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed.update(codec_factor_overrides_from_value(item, key=key))
        return parsed
    raise ToolError(f"{key} must be an object, list, or CODEC=FACTOR string")


def codec_factor_overrides_from_args(args: argparse.Namespace, preset: dict[str, Any]) -> dict[str, float] | None:
    overrides: dict[str, float] = {}
    preset_raw = preset_value(
        preset,
        "codec_source_ratios",
        "codec_source_ratio",
        "codec_hevc_bitrate_factors",
        "codec_factors",
    )
    overrides.update(codec_factor_overrides_from_value(preset_raw, key="codec_source_ratios"))
    overrides.update(codec_factor_overrides_from_value(getattr(args, "codec_source_ratio", None), key="--codec-source-ratio"))
    return overrides or None


def bitrate_options_from_args(args: argparse.Namespace) -> dict[str, Any]:
    preset = load_bitrate_preset_file(getattr(args, "bitrate_preset_file", None))
    preset_mode = preset_value(preset, "bitrate_mode", "mode")
    arg_mode = getattr(args, "bitrate_mode", "balanced")
    if preset_mode is not None and arg_mode == "balanced":
        mode = normalize_bitrate_mode(str(preset_mode))
    else:
        mode = normalize_bitrate_mode(arg_mode)
    return {
        "mode": mode,
        "factor_override": preset_or_arg(args, "hevc_bitrate_factor", None, preset, ("hevc_bitrate_factor", "factor"), preset_float),
        "codec_factor_overrides": codec_factor_overrides_from_args(args, preset),
        "min_bps": preset_or_arg(args, "min_video_bitrate", 2_000_000, preset, ("min_video_bitrate", "min_bps"), preset_bitrate),
        "max_bps": preset_or_arg(args, "max_video_bitrate", 80_000_000, preset, ("max_video_bitrate", "max_bps"), preset_bitrate),
        "maxrate_multiplier": preset_or_arg(args, "maxrate_multiplier", 1.55, preset, ("maxrate_multiplier",), preset_float),
        "bufsize_multiplier": preset_or_arg(args, "bufsize_multiplier", 2.0, preset, ("bufsize_multiplier",), preset_float),
        "anime_cq_min_duration": preset_or_arg(
            args,
            "anime_cq_min_duration",
            DEFAULT_ANIME_CQ_MIN_DURATION,
            preset,
            ("compact_cq_min_duration", "compact-cq-min-duration", "episode_compact_min_duration", "anime_cq_min_duration"),
            preset_duration,
        ),
        "compact_cq_value": preset_or_arg(
            args,
            "compact_cq_value",
            ANIME_CQ_VALUE,
            preset,
            ("compact_cq_value", "compact-cq-value", "cq", "quality"),
            preset_int,
        ),
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
    codec_factor_overrides: dict[str, Any] | None = None,
    min_bps: int = 2_000_000,
    max_bps: int = 80_000_000,
    maxrate_multiplier: float = 1.55,
    bufsize_multiplier: float = 2.0,
    anime_cq_min_duration: float = DEFAULT_ANIME_CQ_MIN_DURATION,
    compact_cq_value: int = ANIME_CQ_VALUE,
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
    if compact_cq_value < 0 or compact_cq_value > 51:
        raise ToolError("--compact-cq-value must be between 0 and 51")
    codec = normalize_codec_key(source_codec)
    codec_factors = codec_factor_overrides_from_value(codec_factor_overrides, key="codec_factor_overrides")
    codec_factor = codec_factors.get(codec)
    if mode == ANIME_CQ_PRESET and factor_override is None and codec_factor is None and (duration_seconds or 0) >= anime_cq_min_duration:
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
            "cq": compact_cq_value,
            "anime_cq_min_duration_seconds": anime_cq_min_duration,
            "compact_cq_value": compact_cq_value,
            "max_mbps": mbps(max_bps),
            "bufsize_multiplier": bufsize_multiplier,
            "source_codec": codec or None,
            "codec_factor_overrides": codec_factors or None,
            "reason": (
                f"compact-cq preset: CQ {compact_cq_value} for clips at least "
                f"{format_duration(anime_cq_min_duration)}"
            ),
        }
    bitrate_mode = "smaller" if mode == ANIME_CQ_PRESET and factor_override is None and codec_factor is None else mode
    if not video_bps or not width or not height or not fps:
        return {
            "target_bps": None,
            "maxrate_bps": None,
            "bufsize_bps": None,
            "factor": None,
            "mode": mode,
            "rate_control": "vbr",
            "source_codec": codec or None,
            "codec_factor_overrides": codec_factors or None,
            "reason": "missing source video bitrate, dimensions, or frame rate",
        }
    bpppf = video_bps / (width * height * fps)
    effective_min_bps = min_bps
    if codec_factor is not None:
        factor = codec_factor
        factor_reason = f"codec-specific HEVC/source bitrate factor for {codec}"
    elif factor_override is not None:
        if factor_override <= 0:
            raise ToolError("--hevc-bitrate-factor must be greater than zero")
        factor = factor_override
        factor_reason = "explicit HEVC/source bitrate factor"
    elif bitrate_mode == "source-ratio":
        factor = 0.60
        factor_reason = "fixed source-ratio HEVC/source bitrate factor"
    else:
        if bpppf < 0.07:
            factor = 0.48
        elif bpppf < 0.12:
            factor = lerp(0.50, 0.53, (bpppf - 0.07) / 0.05)
        elif bpppf < 0.20:
            factor = lerp(0.53, 0.55, (bpppf - 0.12) / 0.08)
        else:
            factor = 0.55
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
        if factor_override is not None or codec_factor is not None:
            factor_reason += "; explicit bitrate factor overrides compact-cq CQ"
        else:
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
        "compact_cq_value": compact_cq_value if mode == ANIME_CQ_PRESET else None,
        "min_mbps": mbps(min_bps),
        "effective_min_mbps": mbps(effective_min_bps),
        "max_mbps": mbps(max_bps),
        "maxrate_multiplier": maxrate_multiplier,
        "bufsize_multiplier": bufsize_multiplier,
        "source_bpppf": round(bpppf, 5),
        "source_codec": codec or None,
        "codec_factor_overrides": codec_factors or None,
        "reason": factor_reason,
    }


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def round_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)
