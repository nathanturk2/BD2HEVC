"""Named preset storage and CLI helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from .bitrate import (
    codec_factor_overrides_from_value,
    parse_bitrate_arg,
    parse_duration_arg,
)
from .config import (
    ANIME_CQ_VALUE,
    DEFAULT_ANIME_CQ_MIN_DURATION,
    DEFAULT_AUDIO_MODE,
    DEFAULT_MONO_AUDIO_BITRATE,
    DEFAULT_STEREO_AUDIO_BITRATE,
    HEVC_ENCODERS,
    ROOT,
)
from .tools import ToolError


PRESET_DIR_ENV = "BD2HEVC_PRESET_DIR"


def user_preset_dir() -> Path:
    override = os.environ.get(PRESET_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "BD2HEVC" / "presets"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "bd2hevc" / "presets"


def bundled_preset_dirs() -> list[Path]:
    return [ROOT / "presets", ROOT / "examples" / "bitrate"]


def preset_search_dirs() -> list[Path]:
    return [user_preset_dir(), *bundled_preset_dirs()]


def normalize_preset_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        raise ToolError("Preset name cannot be empty")
    if any(part in text for part in ("/", "\\", ":")):
        raise ToolError("Preset names cannot contain path separators or drive prefixes")
    if text.lower().endswith(".json"):
        text = text[:-5]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text):
        raise ToolError("Preset names may contain letters, numbers, dots, underscores, and hyphens")
    return text


def named_preset_path(name: str) -> Path:
    return user_preset_dir() / f"{normalize_preset_name(name)}.json"


def resolve_preset_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path).expanduser()
    if candidate.suffix.lower() == ".json" or any(part in name_or_path for part in ("/", "\\", ":")):
        if candidate.exists():
            return candidate
        raise ToolError(f"Preset file not found: {candidate}")
    name = normalize_preset_name(name_or_path)
    for directory in preset_search_dirs():
        path = directory / f"{name}.json"
        if path.exists():
            return path
    searched = ", ".join(str(path) for path in preset_search_dirs())
    raise ToolError(f"Preset not found: {name}. Search locations: {searched}")


def load_preset_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ToolError(f"Could not read preset: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON in preset: {path}") from exc
    if not isinstance(data, dict):
        raise ToolError("Preset must contain a JSON object")
    return data


def load_named_preset(name_or_path: str) -> tuple[Path, dict[str, Any]]:
    path = resolve_preset_path(name_or_path)
    return path, load_preset_json(path)


def parse_bitrate_value(value: Any, key: str) -> int:
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


def parse_duration_value(value: Any, key: str) -> float:
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


def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def set_if_default(args: argparse.Namespace, attr: str, value: Any, default: Any) -> None:
    if value is not None and getattr(args, attr, default) == default:
        setattr(args, attr, value)


def codec_ratio_cli_values(value: Any) -> list[str]:
    parsed = codec_factor_overrides_from_value(value, key="codec_source_ratios")
    return [f"{codec}={factor:g}" for codec, factor in parsed.items()]


def pair_value(value: Any, key: str) -> list[Any]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return [value[0], value[1]]
    raise ToolError(f"{key} must contain two values")


def pair_list_value(value: Any, key: str) -> list[list[Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ToolError(f"{key} must be a list of pairs")
    pairs: list[list[Any]] = []
    for item in value:
        pairs.append(pair_value(item, key))
    return pairs


def clip_list_value(value: Any, key: str) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ToolError(f"{key} must be a list")
    result: list[list[str]] = []
    for item in value:
        if isinstance(item, (list, tuple)):
            result.append([str(part) for part in item])
        else:
            result.append([str(item)])
    return result


def apply_named_preset_to_args(args: argparse.Namespace) -> dict[str, Any] | None:
    name = getattr(args, "preset", None)
    if not name:
        return None
    if getattr(args, "bitrate_preset_file", None):
        raise ToolError("Use either --preset NAME or --preset-file PATH, not both")
    path, data = load_named_preset(name)

    set_if_default(args, "quality", first_present(data, "quality"), None)
    set_if_default(args, "bitrate_mode", first_present(data, "bitrate_mode", "mode"), "balanced")
    set_if_default(args, "hevc_bitrate_factor", first_present(data, "hevc_bitrate_factor", "factor"), None)

    codec_ratios = first_present(data, "codec_source_ratios", "codec_source_ratio", "codec_hevc_bitrate_factors", "codec_factors")
    if codec_ratios is not None:
        existing = getattr(args, "codec_source_ratio", None) or []
        setattr(args, "codec_source_ratio", [*codec_ratio_cli_values(codec_ratios), *existing])

    if (value := first_present(data, "min_video_bitrate", "min_bps")) is not None:
        set_if_default(args, "min_video_bitrate", parse_bitrate_value(value, "min_video_bitrate"), 2_000_000)
    if (value := first_present(data, "max_video_bitrate", "max_bps")) is not None:
        set_if_default(args, "max_video_bitrate", parse_bitrate_value(value, "max_video_bitrate"), 80_000_000)
    if (value := first_present(data, "maxrate_multiplier")) is not None:
        set_if_default(args, "maxrate_multiplier", float(value), 1.55)
    if (value := first_present(data, "bufsize_multiplier")) is not None:
        set_if_default(args, "bufsize_multiplier", float(value), 2.0)
    if (value := first_present(data, "compact_cq_value", "compact-cq-value", "cq")) is not None:
        set_if_default(args, "compact_cq_value", int(value), ANIME_CQ_VALUE)
    if (value := first_present(data, "compact_cq_min_duration", "compact-cq-min-duration", "episode_compact_min_duration", "anime_cq_min_duration")) is not None:
        set_if_default(args, "anime_cq_min_duration", parse_duration_value(value, "compact_cq_min_duration"), DEFAULT_ANIME_CQ_MIN_DURATION)

    set_if_default(args, "main_title_quality", first_present(data, "main_title_quality"), None)
    set_if_default(args, "main_title_bitrate_mode", first_present(data, "main_title_bitrate_mode"), None)
    set_if_default(args, "main_title_cq", first_present(data, "main_title_cq"), None)
    set_if_default(args, "top_n_quality", pair_value(data["top_n_quality"], "top_n_quality") if "top_n_quality" in data else None, None)
    set_if_default(args, "top_n_bitrate_mode", pair_value(data["top_n_bitrate_mode"], "top_n_bitrate_mode") if "top_n_bitrate_mode" in data else None, None)
    set_if_default(args, "top_n_cq", pair_value(data["top_n_cq"], "top_n_cq") if "top_n_cq" in data else None, None)
    set_if_default(args, "clip_quality", pair_list_value(data["clip_quality"], "clip_quality") if "clip_quality" in data else None, None)
    set_if_default(args, "clip_bitrate_mode", pair_list_value(data["clip_bitrate_mode"], "clip_bitrate_mode") if "clip_bitrate_mode" in data else None, None)
    set_if_default(args, "clip_cq", pair_list_value(data["clip_cq"], "clip_cq") if "clip_cq" in data else None, None)
    set_if_default(args, "copy_clips", clip_list_value(data["copy_clips"], "copy_clips") if "copy_clips" in data else None, None)

    if hasattr(args, "audio_mode"):
        set_if_default(args, "audio_mode", first_present(data, "audio_mode"), DEFAULT_AUDIO_MODE)
    if hasattr(args, "stereo_audio_bitrate") and (value := first_present(data, "stereo_audio_bitrate")) is not None:
        set_if_default(args, "stereo_audio_bitrate", parse_bitrate_value(value, "stereo_audio_bitrate"), DEFAULT_STEREO_AUDIO_BITRATE)
    if hasattr(args, "mono_audio_bitrate") and (value := first_present(data, "mono_audio_bitrate")) is not None:
        set_if_default(args, "mono_audio_bitrate", parse_bitrate_value(value, "mono_audio_bitrate"), DEFAULT_MONO_AUDIO_BITRATE)
    if hasattr(args, "encoder"):
        encoder = first_present(data, "encoder")
        if encoder is not None and encoder not in HEVC_ENCODERS:
            raise ToolError(f"Preset encoder must be one of: {', '.join(HEVC_ENCODERS)}")
        set_if_default(args, "encoder", encoder, "hevc_nvenc")
    if hasattr(args, "hevc_bit_depth"):
        set_if_default(args, "hevc_bit_depth", first_present(data, "hevc_bit_depth"), 8)

    setattr(args, "applied_preset", {"name": str(name), "path": str(path)})
    return data


def put_if_set(data: dict[str, Any], key: str, value: Any, default: Any = None) -> None:
    if value is not None and value != default:
        data[key] = value


def preset_data_from_args(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if getattr(args, "description", None):
        data["description"] = args.description
    put_if_set(data, "quality", getattr(args, "quality", None))
    put_if_set(data, "mode", getattr(args, "bitrate_mode", "balanced"), "balanced")
    put_if_set(data, "factor", getattr(args, "hevc_bitrate_factor", None))
    codec_ratios = getattr(args, "codec_source_ratio", None)
    if codec_ratios:
        data["codec_source_ratios"] = codec_factor_overrides_from_value(codec_ratios, key="--codec-source-ratio")
    put_if_set(data, "min_video_bitrate", getattr(args, "min_video_bitrate", 2_000_000), 2_000_000)
    put_if_set(data, "max_video_bitrate", getattr(args, "max_video_bitrate", 80_000_000), 80_000_000)
    put_if_set(data, "maxrate_multiplier", getattr(args, "maxrate_multiplier", 1.55), 1.55)
    put_if_set(data, "bufsize_multiplier", getattr(args, "bufsize_multiplier", 2.0), 2.0)
    put_if_set(data, "compact_cq_value", getattr(args, "compact_cq_value", ANIME_CQ_VALUE), ANIME_CQ_VALUE)
    put_if_set(data, "compact_cq_min_duration", getattr(args, "anime_cq_min_duration", DEFAULT_ANIME_CQ_MIN_DURATION), DEFAULT_ANIME_CQ_MIN_DURATION)
    put_if_set(data, "main_title_quality", getattr(args, "main_title_quality", None))
    put_if_set(data, "main_title_bitrate_mode", getattr(args, "main_title_bitrate_mode", None))
    put_if_set(data, "main_title_cq", getattr(args, "main_title_cq", None))
    put_if_set(data, "top_n_quality", getattr(args, "top_n_quality", None))
    put_if_set(data, "top_n_bitrate_mode", getattr(args, "top_n_bitrate_mode", None))
    put_if_set(data, "top_n_cq", getattr(args, "top_n_cq", None))
    put_if_set(data, "clip_quality", getattr(args, "clip_quality", None))
    put_if_set(data, "clip_bitrate_mode", getattr(args, "clip_bitrate_mode", None))
    put_if_set(data, "clip_cq", getattr(args, "clip_cq", None))
    put_if_set(data, "copy_clips", getattr(args, "copy_clips", None))
    put_if_set(data, "audio_mode", getattr(args, "audio_mode", DEFAULT_AUDIO_MODE), DEFAULT_AUDIO_MODE)
    put_if_set(data, "stereo_audio_bitrate", getattr(args, "stereo_audio_bitrate", DEFAULT_STEREO_AUDIO_BITRATE), DEFAULT_STEREO_AUDIO_BITRATE)
    put_if_set(data, "mono_audio_bitrate", getattr(args, "mono_audio_bitrate", DEFAULT_MONO_AUDIO_BITRATE), DEFAULT_MONO_AUDIO_BITRATE)
    put_if_set(data, "encoder", getattr(args, "encoder", "hevc_nvenc"), "hevc_nvenc")
    put_if_set(data, "hevc_bit_depth", getattr(args, "hevc_bit_depth", 8), 8)
    return data


def available_presets() -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for source, directory in [("user", user_preset_dir()), ("bundled", ROOT / "presets"), ("example", ROOT / "examples" / "bitrate")]:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            data = load_preset_json(path)
            rows.append({"name": name, "source": source, "path": path, "description": data.get("description")})
    return rows


def cmd_preset_list(args: argparse.Namespace) -> int:
    rows = available_presets()
    if getattr(args, "json", False):
        print(json.dumps([{**row, "path": str(row["path"])} for row in rows], indent=2))
        return 0
    if not rows:
        print("No presets saved yet.")
        print(f"Preset folder: {user_preset_dir()}")
        return 0
    print("BD2HEVC presets")
    for row in rows:
        detail = f" - {row['description']}" if row.get("description") else ""
        print(f"{row['name']}  ({row['source']}){detail}")
    print(f"User preset folder: {user_preset_dir()}")
    return 0


def cmd_preset_show(args: argparse.Namespace) -> int:
    path, data = load_named_preset(args.name)
    if getattr(args, "json", False):
        print(json.dumps({"name": Path(path).stem, "path": str(path), "preset": data}, indent=2))
        return 0
    print(f"Preset: {Path(path).stem}")
    print(f"Path: {path}")
    print(json.dumps(data, indent=2))
    return 0


def cmd_preset_save(args: argparse.Namespace) -> int:
    name = normalize_preset_name(args.name)
    path = named_preset_path(name)
    if path.exists() and not getattr(args, "force", False):
        raise ToolError(f"Preset already exists: {name}. Use --force to replace it.")
    data = preset_data_from_args(args)
    if not any(key != "description" for key in data):
        raise ToolError("No preset settings were supplied")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps({"name": name, "path": str(path), "preset": data}, indent=2))
    else:
        print(f"Preset saved: {name}")
        print(f"Path: {path}")
        print(f"Use it with: python bd2hevc.py queue \"BD backups\" --output-dir \"Converted UHD-BD\" --preset {name}")
    return 0


def cmd_preset_remove(args: argparse.Namespace) -> int:
    path = named_preset_path(args.name)
    if not path.exists():
        raise ToolError(f"User preset not found: {normalize_preset_name(args.name)}")
    path.unlink()
    print(f"Preset removed: {normalize_preset_name(args.name)}")
    return 0
