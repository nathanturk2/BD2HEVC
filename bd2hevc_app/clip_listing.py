"""Clip listing: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def planned_clip_quality_text(clip: dict[str, Any], *, services) -> str:
    action = clip.get("action")
    original_action = services.original_clip_action(clip)
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


def planned_clip_output_codec(clip: dict[str, Any], *, services) -> str | None:
    video = clip.get("video") or {}
    source_codec = video.get("codec_name")
    action = clip.get("action")
    if action == "reencode":
        return "hevc"
    if action in {"copy", "already_hevc"}:
        return source_codec
    return None


def field_order_label(field_order: Any, *, services) -> str:
    text = str(field_order or "").strip().lower()
    if not text or text == "unknown":
        return "-"
    if text == "progressive":
        return "prog"
    if text in {"tt", "bb", "tb", "bt"}:
        return text
    if "top" in text:
        return "top"
    if "bottom" in text:
        return "bottom"
    return text[:7]


def clip_list_rows(clips: list[dict[str, Any]], *, sort: str = "duration", services) -> list[dict[str, Any]]:
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
                "duration_text": services.format_duration(clip.get("duration")),
                "planned_action": clip.get("action"),
                "original_action": services.original_clip_action(clip),
                "codec": video.get("codec_name"),
                "source_codec": video.get("codec_name"),
                "planned_codec": services.planned_clip_output_codec(clip),
                "source_video_mbps": video.get("source_video_bitrate_mbps"),
                "planned_quality": services.planned_clip_quality_text(clip),
                "field_order": video.get("field_order"),
                "postprocess": video.get("postprocess"),
            }
        )
    return rows


def print_clip_list(rows: list[dict[str, Any]], *, services) -> None:
    print(f"{'clip':<12} {'duration':>8} {'action':<12} {'source':<10} {'field':<7} {'output':<8} {'src Mbps':>8}  quality")
    print(f"{'-' * 12} {'-' * 8} {'-' * 12} {'-' * 10} {'-' * 7} {'-' * 8} {'-' * 8}  {'-' * 24}")
    for row in rows:
        mbps_text = "" if row.get("source_video_mbps") is None else str(row.get("source_video_mbps"))
        postprocess = row.get("postprocess") or {}
        quality = row.get("planned_quality") or ""
        if (postprocess.get("deinterlace") or {}).get("enabled"):
            quality = f"{quality}; deinterlace".strip("; ")
        field = services.field_order_label(row.get("field_order"))
        print(
            f"{str(row.get('clip') or ''):<12} "
            f"{str(row.get('duration_text') or ''):>8} "
            f"{str(row.get('planned_action') or ''):<12} "
            f"{str(row.get('source_codec') or row.get('codec') or ''):<10} "
            f"{field:<7} "
            f"{str(row.get('planned_codec') or ''):<8} "
            f"{mbps_text:>8}  "
            f"{quality}"
        )
