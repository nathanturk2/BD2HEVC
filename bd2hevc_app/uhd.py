"""UHD-BD physical-layout and size-target helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .bitrate import format_duration, mbps, safe_float, safe_int
from .tools import ToolError


DISC_SIZE_BYTES = {
    "bd25": 25_000_000_000,
    "bd50": 50_000_000_000,
    "bd66": 66_000_000_000,
    "bd100": 100_000_000_000,
}

UHD_BDMV_DIRS = [
    Path("BDMV"),
    Path("BDMV") / "AUXDATA",
    Path("BDMV") / "BACKUP",
    Path("BDMV") / "BACKUP" / "BDJO",
    Path("BDMV") / "BACKUP" / "CLIPINF",
    Path("BDMV") / "BACKUP" / "JAR",
    Path("BDMV") / "BACKUP" / "PLAYLIST",
    Path("BDMV") / "BDJO",
    Path("BDMV") / "CLIPINF",
    Path("BDMV") / "JAR",
    Path("BDMV") / "META",
    Path("BDMV") / "PLAYLIST",
    Path("BDMV") / "STREAM",
    Path("CERTIFICATE"),
    Path("CERTIFICATE") / "BACKUP",
]

VERSION_PATCHES_TO_UHD = {
    b"INDX0200": b"INDX0300",
    b"MOBJ0200": b"MOBJ0300",
    b"BDJO0200": b"BDJO0300",
    b"MPLS0200": b"MPLS0300",
    b"HDMV0200": b"HDMV0300",
}

VERSION_PATCHES_TO_BD = {new: old for old, new in VERSION_PATCHES_TO_UHD.items()}


def parse_disc_size(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ToolError("Disc target size must be greater than zero")
        return int(value)
    text = str(value).strip().lower()
    if not text or text in {"none", "off"}:
        return None
    if text in DISC_SIZE_BYTES:
        return DISC_SIZE_BYTES[text]
    multiplier = 1
    number = text
    if text.endswith("gib"):
        multiplier = 1024**3
        number = text[:-3]
    elif text.endswith("gb"):
        multiplier = 1_000_000_000
        number = text[:-2]
    elif text.endswith("mib"):
        multiplier = 1024**2
        number = text[:-3]
    elif text.endswith("mb"):
        multiplier = 1_000_000
        number = text[:-2]
    try:
        parsed = float(number.strip())
    except ValueError as exc:
        raise ToolError(f"Invalid disc target size: {value}") from exc
    if parsed <= 0:
        raise ToolError("Disc target size must be greater than zero")
    return int(parsed * multiplier)


def tree_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        root_path = Path(root)
        for name in files:
            try:
                total += (root_path / name).stat().st_size
            except OSError:
                pass
    return total


def stream_path(source_root: Path, clip_file: str) -> Path:
    return source_root / "BDMV" / "STREAM" / clip_file


def estimated_source_video_bytes(clip: dict[str, Any]) -> int:
    video = clip.get("video") or {}
    duration = safe_float(clip.get("duration")) or 0.0
    source_bps = safe_int(video.get("source_video_bitrate")) or safe_int(video.get("bit_rate")) or 0
    return int(max(0, source_bps) * max(0.0, duration) / 8)


def estimated_passthrough_nonvideo_bytes(clip: dict[str, Any], source_clip_bytes: int, audio_mode: str) -> int:
    duration = safe_float(clip.get("duration")) or 0.0
    nonvideo = max(0, source_clip_bytes - estimated_source_video_bytes(clip))
    if audio_mode != "compact-stereo" or duration <= 0:
        return nonvideo
    compact_audio = 0
    for audio in clip.get("audio") or []:
        channels = safe_int(audio.get("channels")) or 0
        if channels <= 0:
            continue
        compact_audio += (128_000 if channels == 1 else 256_000) * duration / 8
    # Keep a subtitle/container allowance from the original non-video payload.
    allowance = min(nonvideo, max(2_000_000, int(nonvideo * 0.08)))
    return int(compact_audio + allowance)


def estimated_reencoded_clip_bytes(clip: dict[str, Any], source_clip_bytes: int, audio_mode: str) -> tuple[int, int]:
    video = clip.get("video") or {}
    target = video.get("target_hevc") or {}
    duration = safe_float(clip.get("duration")) or 0.0
    target_bps = safe_int(target.get("target_bps"))
    if target_bps is None:
        raise ToolError(
            "Disc-size fitting needs VBR targets. Use --quality balanced, smaller, transparent, "
            "source-ratio:N, or --hevc-bitrate-factor instead of CQ for --target-disc-size."
        )
    target_video = int(target_bps * max(0.0, duration) / 8)
    return target_video, target_video + estimated_passthrough_nonvideo_bytes(clip, source_clip_bytes, audio_mode)


def fit_reencoded_clips_to_disc_size(
    source_root: Path,
    clips: list[dict[str, Any]],
    *,
    target_size: str | int | None,
    margin: float = 0.98,
    audio_mode: str = "passthrough",
) -> dict[str, Any] | None:
    capacity = parse_disc_size(target_size)
    if capacity is None:
        return None
    if not 0.50 <= margin <= 1.0:
        raise ToolError("--target-disc-margin must be between 0.50 and 1.0")
    source_total = tree_size(source_root)
    selected = [clip for clip in clips if clip.get("action") == "reencode"]
    source_clip_sizes: dict[str, int] = {}
    skipped_source_bytes = 0
    target_video_bytes = 0
    target_clip_bytes = 0
    for clip in selected:
        clip_file = str(clip.get("file") or "")
        size = stream_path(source_root, clip_file).stat().st_size
        source_clip_sizes[clip_file] = size
        skipped_source_bytes += size
        video_bytes, clip_bytes = estimated_reencoded_clip_bytes(clip, size, audio_mode)
        target_video_bytes += video_bytes
        target_clip_bytes += clip_bytes
    fixed_bytes = max(0, source_total - skipped_source_bytes)
    estimated_total = int((fixed_bytes + target_clip_bytes) * 1.01)
    budget = int(capacity * margin)
    report: dict[str, Any] = {
        "target_size": target_size,
        "capacity_bytes": capacity,
        "budget_bytes": budget,
        "margin": margin,
        "source_total_bytes": source_total,
        "fixed_non_reencoded_bytes": fixed_bytes,
        "estimated_before_bytes": estimated_total,
        "selected_reencoded_clips": len(selected),
        "scaled": False,
    }
    if estimated_total <= budget or not selected:
        report["reason"] = "estimated output already fits target" if selected else "no reencoded clips"
        return report
    nonvariable_bytes = estimated_total - target_video_bytes
    available_video_bytes = budget - nonvariable_bytes
    if available_video_bytes <= 0:
        raise ToolError(
            f"Cannot fit disc target {target_size}: non-video/copied data alone is about "
            f"{nonvariable_bytes / 1_000_000_000:.2f} GB against a {budget / 1_000_000_000:.2f} GB budget."
        )
    scale = max(0.01, available_video_bytes / max(1, target_video_bytes))
    if scale >= 1.0:
        report["reason"] = "target-video estimate was already within budget after fixed-data accounting"
        return report
    scaled_video_bytes = 0
    clip_reports: list[dict[str, Any]] = []
    for clip in selected:
        video = clip.get("video") or {}
        target = video.get("target_hevc") or {}
        old_bps = safe_int(target.get("target_bps")) or 0
        duration = safe_float(clip.get("duration")) or 0.0
        min_bps = int((target.get("effective_min_mbps") or target.get("min_mbps") or 0) * 1_000_000) if target.get("effective_min_mbps") or target.get("min_mbps") else 300_000
        new_bps = max(min_bps, int(round(old_bps * scale / 100_000) * 100_000))
        target["target_bps"] = new_bps
        target["target_mbps"] = mbps(new_bps)
        maxrate_multiplier = safe_float(target.get("maxrate_multiplier")) or 1.55
        bufsize_multiplier = safe_float(target.get("bufsize_multiplier")) or 2.0
        maxrate = int(round(max(new_bps * maxrate_multiplier, new_bps + 2_000_000) / 100_000) * 100_000)
        bufsize = int(round(maxrate * bufsize_multiplier / 100_000) * 100_000)
        target["maxrate_bps"] = maxrate
        target["maxrate_mbps"] = mbps(maxrate)
        target["bufsize_bps"] = bufsize
        target["bufsize_mbps"] = mbps(bufsize)
        reason = target.get("reason")
        suffix = f"disc-size fit target {target_size}: scaled VBR target by {scale:.3f}"
        target["reason"] = f"{reason}; {suffix}" if reason else suffix
        scaled_video_bytes += int(new_bps * max(0.0, duration) / 8)
        clip_reports.append(
            {
                "file": clip.get("file"),
                "duration": format_duration(duration),
                "old_target_mbps": mbps(old_bps),
                "new_target_mbps": mbps(new_bps),
            }
        )
    estimated_after = int(nonvariable_bytes + scaled_video_bytes)
    report.update(
        {
            "scaled": True,
            "scale": round(scale, 4),
            "estimated_after_bytes": estimated_after,
            "estimated_after_fits": estimated_after <= budget,
            "clips": clip_reports,
        }
    )
    if estimated_after > budget:
        report["warning"] = "minimum bitrate limits prevented the estimate from fitting the requested disc target"
    return report


def patch_version_header(path: Path, *, target: str = "uhd") -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"file": str(path), "exists": False, "patched": False}
    if target not in {"uhd", "bd"}:
        raise ToolError("Version header target must be 'uhd' or 'bd'")
    patches = VERSION_PATCHES_TO_UHD if target == "uhd" else VERSION_PATCHES_TO_BD
    data = path.read_bytes()
    for old, new in patches.items():
        if data.startswith(old):
            if old == new:
                break
            path.write_bytes(new + data[len(new) :])
            return {"file": str(path), "exists": True, "patched": True, "target": target, "old": old.decode(), "new": new.decode()}
    return {"file": str(path), "exists": True, "patched": False, "target": target, "header": data[:8].decode("ascii", errors="replace")}


def mirror_if_missing(primary: Path, backup: Path) -> dict[str, Any]:
    if backup.exists():
        return {"primary": str(primary), "backup": str(backup), "created": False, "reason": "backup exists"}
    if not primary.exists():
        return {"primary": str(primary), "backup": str(backup), "created": False, "reason": "primary missing"}
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(primary, backup)
    return {"primary": str(primary), "backup": str(backup), "created": True}


def mirror_directory_files_if_missing(primary_dir: Path, backup_dir: Path, pattern: str) -> list[dict[str, Any]]:
    reports = []
    if not primary_dir.exists():
        return [{"primary": str(primary_dir), "backup": str(backup_dir), "created": False, "reason": "primary directory missing"}]
    for primary in sorted(primary_dir.glob(pattern)):
        if primary.is_file():
            reports.append(mirror_if_missing(primary, backup_dir / primary.name))
    return reports


def ensure_uhd_backup_structure(root: Path, *, patch_version_headers: bool = False) -> dict[str, Any]:
    root = root.resolve()
    created_dirs = []
    for relative in UHD_BDMV_DIRS:
        folder = root / relative
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(relative))

    mirrored = [
        mirror_if_missing(root / "BDMV" / "index.bdmv", root / "BDMV" / "BACKUP" / "index.bdmv"),
        mirror_if_missing(root / "BDMV" / "MovieObject.bdmv", root / "BDMV" / "BACKUP" / "MovieObject.bdmv"),
        mirror_if_missing(root / "CERTIFICATE" / "id.bdmv", root / "CERTIFICATE" / "BACKUP" / "id.bdmv"),
    ]
    mirrored.extend(mirror_directory_files_if_missing(root / "BDMV" / "PLAYLIST", root / "BDMV" / "BACKUP" / "PLAYLIST", "*.mpls"))
    mirrored.extend(mirror_directory_files_if_missing(root / "BDMV" / "CLIPINF", root / "BDMV" / "BACKUP" / "CLIPINF", "*.clpi"))
    mirrored.extend(mirror_directory_files_if_missing(root / "BDMV" / "BDJO", root / "BDMV" / "BACKUP" / "BDJO", "*.bdjo"))
    mirrored.extend(mirror_directory_files_if_missing(root / "BDMV" / "JAR", root / "BDMV" / "BACKUP" / "JAR", "*.jar"))

    version_files = [
        root / "BDMV" / "index.bdmv",
        root / "BDMV" / "MovieObject.bdmv",
        root / "BDMV" / "BACKUP" / "index.bdmv",
        root / "BDMV" / "BACKUP" / "MovieObject.bdmv",
    ]
    version_files.extend((root / "BDMV" / "BDJO").glob("*.bdjo"))
    version_files.extend((root / "BDMV" / "BACKUP" / "BDJO").glob("*.bdjo"))
    version_files.extend((root / "BDMV" / "PLAYLIST").glob("*.mpls"))
    version_files.extend((root / "BDMV" / "BACKUP" / "PLAYLIST").glob("*.mpls"))
    version_files.extend((root / "BDMV" / "CLIPINF").glob("*.clpi"))
    version_files.extend((root / "BDMV" / "BACKUP" / "CLIPINF").glob("*.clpi"))
    version_target = "uhd" if patch_version_headers else "bd"
    versions = [patch_version_header(path, target=version_target) for path in version_files]

    required_files = [
        root / "BDMV" / "index.bdmv",
        root / "BDMV" / "MovieObject.bdmv",
        root / "BDMV" / "BACKUP" / "index.bdmv",
        root / "BDMV" / "BACKUP" / "MovieObject.bdmv",
    ]
    missing_required = [str(path.relative_to(root)) for path in required_files if not path.exists()]
    certificate_id = root / "CERTIFICATE" / "id.bdmv"
    certificate_backup_id = root / "CERTIFICATE" / "BACKUP" / "id.bdmv"
    return {
        "root": str(root),
        "created_dirs": created_dirs,
        "mirrored": mirrored,
        "version_header_target": version_target,
        "version_patches": versions,
        "missing_required_files": missing_required,
        "certificate": {
            "directory_exists": (root / "CERTIFICATE").is_dir(),
            "id_bdmv_exists": certificate_id.exists(),
            "backup_id_bdmv_exists": certificate_backup_id.exists(),
            "note": "Encryption/AACS material is not generated.",
        },
    }
