"""Blu-ray navigation metadata repair helpers."""

from __future__ import annotations

import shutil
import tempfile
import json
from pathlib import Path
from typing import Any

from .config import (
    CLPI_PRIMARY_VIDEO_AVC,
    CLPI_PRIMARY_VIDEO_HEVC,
    CLPI_PRIMARY_VIDEO_MPEG2,
    MPLS_PRIMARY_VIDEO_AVC,
    MPLS_PRIMARY_VIDEO_HEVC,
    MPLS_PRIMARY_VIDEO_MPEG2,
    ROOT,
)
from .muxing import write_tsmuxer_meta
from .scan import inspect_clip
from .tools import ToolError, require_tool, run_cmd

def read_be16(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def read_be32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def replace_in_range(data: bytearray, start: int, end: int, old: bytes, new: bytes) -> int:
    count = 0
    pos = data.find(old, start, end)
    while pos != -1:
        data[pos : pos + len(old)] = new
        count += 1
        pos = data.find(old, pos + len(new), end)
    return count


def parse_mpls_play_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = path.read_bytes()
    if not data.startswith(b"MPLS") or len(data) < 20:
        return []
    playlist_start = read_be32(data, 8)
    if playlist_start <= 0 or playlist_start + 10 > len(data):
        return []
    playlist_length = read_be32(data, playlist_start)
    playlist_end = min(len(data), playlist_start + 4 + playlist_length)
    item_count = read_be16(data, playlist_start + 6)
    pos = playlist_start + 10
    items: list[dict[str, Any]] = []
    for index in range(item_count):
        if pos + 2 > playlist_end:
            break
        item_length = read_be16(data, pos)
        item_end = min(len(data), pos + 2 + item_length)
        clip_id = data[pos + 2 : pos + 7].decode("ascii", errors="ignore") if pos + 7 <= item_end else ""
        in_time = read_be32(data, pos + 14) if pos + 18 <= item_end else None
        out_time = read_be32(data, pos + 18) if pos + 22 <= item_end else None
        duration = None
        if in_time is not None and out_time is not None and out_time >= in_time:
            duration = (out_time - in_time) / 45_000.0
        items.append(
            {
                "index": index,
                "clip_id": clip_id,
                "in_time": in_time,
                "out_time": out_time,
                "duration": duration,
            }
        )
        pos = item_end
    return items


def short_repeated_playitem_clips(disc_root: Path, clip_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    bdmv = disc_root / "BDMV"
    candidates: dict[str, list[dict[str, Any]]] = {}
    playlist_dir = bdmv / "PLAYLIST"
    if not playlist_dir.is_dir():
        return candidates
    for playlist in sorted(playlist_dir.glob("*.mpls")):
        items = parse_mpls_play_items(playlist)
        by_clip: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            clip_id = str(item.get("clip_id") or "")
            if clip_id in clip_ids:
                by_clip.setdefault(clip_id, []).append(item)
        for clip_id, clip_items in by_clip.items():
            durations = [float(item["duration"]) for item in clip_items if item.get("duration") is not None]
            if len(clip_items) >= 3 and durations and max(durations) <= 5.0:
                candidates.setdefault(clip_id, []).append(
                    {
                        "playlist": str(playlist),
                        "playitem_count": len(clip_items),
                        "max_playitem_seconds": max(durations),
                    }
                )
    return candidates


def patch_clpi_for_hevc(path: Path, *, patch_version_headers: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {"file": str(path), "exists": False, "patched": False}
    data = bytearray(path.read_bytes())
    original = bytes(data)
    version_changed = False
    if patch_version_headers and data.startswith(b"HDMV0200"):
        data[4:8] = b"0300"
        version_changed = True
    stream_patches = 0
    stream_patches += patch_clpi_primary_video_descriptors(data)
    if data != original:
        path.write_bytes(data)
    return {
        "file": str(path),
        "exists": True,
        "patched": data != original,
        "version_changed": version_changed,
        "primary_video_patches": stream_patches,
    }


def patch_clpi_primary_video_descriptors(data: bytearray) -> int:
    patches = 0
    prefix = CLPI_PRIMARY_VIDEO_HEVC[:4]
    for index in range(0, max(0, len(data) - len(prefix) - 1)):
        if data[index : index + len(prefix)] != prefix:
            continue
        if data[index + len(prefix)] not in (CLPI_PRIMARY_VIDEO_AVC[4], CLPI_PRIMARY_VIDEO_MPEG2[4]):
            continue
        data[index + len(prefix)] = CLPI_PRIMARY_VIDEO_HEVC[4]
        patches += 1
    return patches


def clpi_offsets(data: bytes | bytearray) -> dict[str, int]:
    if not data.startswith(b"HDMV") or len(data) < 28:
        raise ToolError("Not a CLPI file")
    return {
        "sequence_info": read_be32(data, 8),
        "program_info": read_be32(data, 12),
        "cpi": read_be32(data, 16),
        "clip_mark": read_be32(data, 20),
        "extension_data": read_be32(data, 24),
    }


def write_be32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = int(value).to_bytes(4, "big")


def read_bits(data: bytes | bytearray, bit_offset: int, bit_count: int) -> int:
    value = 0
    for index in range(bit_count):
        pos = bit_offset + index
        value = (value << 1) | ((data[pos // 8] >> (7 - (pos % 8))) & 1)
    return value


def write_bits(data: bytearray, bit_offset: int, bit_count: int, value: int) -> None:
    if value < 0 or value >= (1 << bit_count):
        raise ToolError(f"Bit-field value out of range at bit {bit_offset}: {value}")
    for index in range(bit_count):
        pos = bit_offset + index
        mask = 1 << (7 - (pos % 8))
        if (value >> (bit_count - 1 - index)) & 1:
            data[pos // 8] |= mask
        else:
            data[pos // 8] &= ~mask


CLPI_SOURCE_PACKET_OFFSET = 0x38
CLPI_FINE_SPN_MASK = 0x1FFFF


def parse_clpi_cpi_entries(data: bytes | bytearray) -> list[dict[str, Any]]:
    offsets = clpi_offsets(data)
    cpi = offsets["cpi"]
    clip_mark = offsets["clip_mark"]
    if not (0 <= cpi + 8 <= clip_mark <= len(data)):
        raise ToolError("Invalid CLPI CPI offsets")
    ep_map = cpi + 6
    if ep_map + 2 > clip_mark:
        return []
    stream_count = data[ep_map + 1]
    bit = (ep_map + 2) * 8
    entries: list[dict[str, Any]] = []
    for _ in range(stream_count):
        if bit + 96 > clip_mark * 8:
            break
        pid = read_bits(data, bit, 16)
        bit += 16
        bit += 10
        ep_stream_type = read_bits(data, bit, 4)
        bit += 4
        num_coarse = read_bits(data, bit, 16)
        bit += 16
        num_fine = read_bits(data, bit, 18)
        bit += 18
        relative_ep_start = read_bits(data, bit, 32)
        bit += 32
        ep_start = ep_map + relative_ep_start
        if ep_start + 4 > clip_mark:
            continue
        fine_start = read_be32(data, ep_start)
        coarse_start = ep_start + 4
        fine_table = ep_start + fine_start
        if coarse_start + (num_coarse * 8) > clip_mark or fine_table + (num_fine * 4) > clip_mark:
            continue
        coarse: list[dict[str, int]] = []
        for index in range(num_coarse):
            item_bit = (coarse_start + index * 8) * 8
            coarse.append(
                {
                    "index": index,
                    "bit": item_bit,
                    "ref": read_bits(data, item_bit, 18),
                    "pts": read_bits(data, item_bit + 18, 14),
                    "spn": read_bits(data, item_bit + 32, 32),
                }
            )
        fine: list[dict[str, int]] = []
        for index in range(num_fine):
            item_bit = (fine_table + index * 4) * 8
            fine.append(
                {
                    "index": index,
                    "bit": item_bit,
                    "angle_change": read_bits(data, item_bit, 1),
                    "end_position_offset": read_bits(data, item_bit + 1, 3),
                    "pts": read_bits(data, item_bit + 4, 11),
                    "spn": read_bits(data, item_bit + 15, 17),
                }
            )
        entries.append(
            {
                "pid": pid,
                "type": ep_stream_type,
                "coarse": coarse,
                "fine": fine,
            }
        )
    return entries


def clpi_cpi_actual_entries(entry: dict[str, Any]) -> list[tuple[int, int, int]]:
    coarse = entry.get("coarse") or []
    fine = entry.get("fine") or []
    values: list[tuple[int, int, int]] = []
    for coarse_index, coarse_item in enumerate(coarse):
        start = int(coarse_item.get("ref") or 0)
        end = int(coarse[coarse_index + 1].get("ref") or 0) if coarse_index + 1 < len(coarse) else len(fine)
        base_spn = int(coarse_item.get("spn") or 0) & ~CLPI_FINE_SPN_MASK
        pts_high = int(coarse_item.get("pts") or 0) << 11
        for fine_index in range(start, min(end, len(fine))):
            fine_item = fine[fine_index]
            values.append((fine_index, pts_high + int(fine_item.get("pts") or 0), base_spn + int(fine_item.get("spn") or 0)))
    return sorted(values, key=lambda item: item[0])


def cpi_ref_spn_overflow(refs: list[int], scaled_entries: list[tuple[int, int, int]], fine_count: int) -> int:
    scaled_by_fine = {fine_index: (full_pts, spn) for fine_index, full_pts, spn in scaled_entries}
    overflow = 0
    boundaries = refs + [fine_count]
    for start, end in zip(boundaries, boundaries[1:]):
        if start not in scaled_by_fine:
            continue
        _, first_spn = scaled_by_fine[start]
        base_spn = first_spn & ~CLPI_FINE_SPN_MASK
        for fine_index in range(start, end):
            if fine_index not in scaled_by_fine:
                continue
            _, spn = scaled_by_fine[fine_index]
            if spn < base_spn:
                overflow += base_spn - spn
            elif spn - base_spn > CLPI_FINE_SPN_MASK:
                overflow += spn - base_spn - CLPI_FINE_SPN_MASK
    return overflow


def build_scaled_cpi_refs(scaled_entries: list[tuple[int, int, int]], coarse_count: int, fine_count: int) -> list[int]:
    if not scaled_entries:
        return []
    full_pts_by_fine = {fine_index: full_pts for fine_index, full_pts, _ in scaled_entries}
    refs = [scaled_entries[0][0]]
    last_pts_high = scaled_entries[0][1] >> 11
    last_spn_base = scaled_entries[0][2] & ~CLPI_FINE_SPN_MASK
    for fine_index, full_pts, spn in scaled_entries[1:]:
        pts_high = full_pts >> 11
        spn_base = spn & ~CLPI_FINE_SPN_MASK
        if pts_high != last_pts_high or spn_base != last_spn_base:
            refs.append(fine_index)
            last_pts_high = pts_high
            last_spn_base = spn_base
    while len(refs) > coarse_count:
        candidates: list[tuple[int, int, int, list[int]]] = []
        for index in range(1, len(refs)):
            ref = refs[index]
            previous_ref = refs[index - 1]
            ref_pts_high = (full_pts_by_fine.get(ref, 0) >> 11)
            previous_pts_high = (full_pts_by_fine.get(previous_ref, 0) >> 11)
            pts_penalty = 0 if ref_pts_high == previous_pts_high else 1_000_000_000
            candidate = refs[:index] + refs[index + 1 :]
            candidates.append((pts_penalty, cpi_ref_spn_overflow(candidate, scaled_entries, fine_count), ref, candidate))
        if not candidates:
            raise ToolError(f"Scaled CLPI CPI needs more coarse entries than the source CLPI has: {coarse_count}")
        _, _, _, refs = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    while len(refs) < coarse_count:
        boundaries = refs + [fine_count]
        candidates = [(end - start, start, end) for start, end in zip(boundaries, boundaries[1:]) if end - start > 2]
        if not candidates:
            raise ToolError(f"Could not safely fill {coarse_count} CLPI CPI coarse entries")
        _, start, end = max(candidates)
        refs.append((start + end) // 2)
        refs = sorted(set(refs))
    return refs


def output_video_keyframe_spns(output_clip: Path, tools: dict[str, Any]) -> dict[str, Any]:
    ffprobe = require_tool(tools, "ffprobe")
    result = run_cmd(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pkt_pos,best_effort_timestamp_time,pts_time,pict_type",
            "-of",
            "json",
            str(output_clip),
        ],
        check=True,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"Could not parse ffprobe keyframe output for {output_clip}") from exc
    keyframes: list[dict[str, Any]] = []
    for frame in payload.get("frames") or []:
        pos = frame.get("pkt_pos")
        if pos is None:
            continue
        timestamp = frame.get("best_effort_timestamp_time") or frame.get("pts_time")
        keyframes.append(
            {
                "spn": int(pos) // 192,
                "timestamp": float(timestamp) if timestamp is not None else None,
                "pict_type": frame.get("pict_type"),
            }
        )
    return {"clip": str(output_clip), "keyframes": keyframes, "keyframe_count": len(keyframes)}


def actual_keyframe_spn_entries(
    actual_entries: list[tuple[int, int, int]],
    keyframes: list[dict[str, Any]],
) -> list[tuple[int, int, int]] | None:
    if not actual_entries:
        return []
    surplus = len(keyframes) - len(actual_entries)
    allowed_surplus = max(1, int(len(actual_entries) * 0.05))
    if surplus < 0 or surplus > allowed_surplus:
        return None
    mapped = []
    previous_spn = -1
    for index, (fine_index, full_pts, _source_spn) in enumerate(actual_entries):
        spn = int(keyframes[index]["spn"])
        if spn < previous_spn:
            return None
        mapped.append((fine_index, full_pts, spn))
        previous_spn = spn
    return mapped


def write_cpi_spn_entries(data: bytearray, entry: dict[str, Any], mapped_entries: list[tuple[int, int, int]]) -> dict[str, Any]:
    coarse = entry.get("coarse") or []
    fine = entry.get("fine") or []
    refs = build_scaled_cpi_refs(mapped_entries, len(coarse), len(fine))
    mapped_by_fine = {fine_index: (full_pts, spn) for fine_index, full_pts, spn in mapped_entries}
    approximated_fine_entries = 0
    for coarse_index, coarse_item in enumerate(coarse):
        ref = refs[coarse_index]
        next_ref = refs[coarse_index + 1] if coarse_index + 1 < len(refs) else len(fine)
        full_pts, first_spn = mapped_by_fine[ref]
        base_spn = first_spn & ~CLPI_FINE_SPN_MASK
        write_bits(data, int(coarse_item["bit"]), 18, ref)
        write_bits(data, int(coarse_item["bit"]) + 18, 14, full_pts >> 11)
        write_bits(data, int(coarse_item["bit"]) + 32, 32, first_spn)
        for fine_index in range(ref, next_ref):
            if fine_index not in mapped_by_fine:
                continue
            _, spn = mapped_by_fine[fine_index]
            low_spn = spn - base_spn
            if low_spn < 0 or low_spn > CLPI_FINE_SPN_MASK:
                approximated_fine_entries += 1
                low_spn = max(0, min(CLPI_FINE_SPN_MASK, low_spn))
            write_bits(data, int(fine[fine_index]["bit"]) + 15, 17, low_spn)
    return {"refs": refs, "approximated_fine_entries": approximated_fine_entries}


def scale_clpi_cpi_map_to_stream(
    source_clip: Path,
    output_clip: Path,
    output_clpi: Path,
    *,
    tools: dict[str, Any] | None = None,
    prefer_actual_keyframe_spns: bool = False,
) -> dict[str, Any]:
    if not output_clpi.exists():
        return {"clpi": str(output_clpi), "scaled": False, "missing_clpi": True}
    if not source_clip.exists() or not output_clip.exists():
        return {
            "clpi": str(output_clpi),
            "scaled": False,
            "missing_source_clip": not source_clip.exists(),
            "missing_output_clip": not output_clip.exists(),
        }
    source_packets = source_clip.stat().st_size // 192
    output_packets = output_clip.stat().st_size // 192
    if source_packets <= 0 or output_packets <= 0:
        return {"clpi": str(output_clpi), "scaled": False, "source_packets": source_packets, "output_packets": output_packets}
    data = bytearray(output_clpi.read_bytes())
    if len(data) <= CLPI_SOURCE_PACKET_OFFSET + 4:
        return {"clpi": str(output_clpi), "scaled": False, "reason": "clpi too small"}
    old_num_source_packets = read_be32(data, CLPI_SOURCE_PACKET_OFFSET)
    ratio = output_packets / source_packets
    entries = parse_clpi_cpi_entries(data)
    keyframe_report = None
    keyframes: list[dict[str, Any]] = []
    if prefer_actual_keyframe_spns and tools:
        try:
            keyframe_report = output_video_keyframe_spns(output_clip, tools)
            keyframes = keyframe_report.get("keyframes") or []
        except Exception as exc:
            keyframe_report = {"clip": str(output_clip), "error": str(exc), "keyframes": [], "keyframe_count": 0}
    stream_reports: list[dict[str, Any]] = []
    write_be32(data, CLPI_SOURCE_PACKET_OFFSET, output_packets)
    for entry in entries:
        actual = clpi_cpi_actual_entries(entry)
        if not actual:
            continue
        mapping = "scaled-ratio"
        mapped = None
        if keyframes:
            mapped = actual_keyframe_spn_entries(actual, keyframes)
            if mapped is not None:
                mapping = "actual-keyframe-spn"
        if mapped is None:
            mapped = [
                (
                    fine_index,
                    full_pts,
                    max(0, min(output_packets - 1, int(round(spn * ratio)))),
                )
                for fine_index, full_pts, spn in actual
            ]
        write_report = write_cpi_spn_entries(data, entry, mapped)
        stream_reports.append(
            {
                "pid": entry.get("pid"),
                "type": entry.get("type"),
                "mapping": mapping,
                "refs": write_report.get("refs"),
                "fine_entries": len(mapped),
                "approximated_fine_entries": write_report.get("approximated_fine_entries"),
                "min_spn": min(item[2] for item in mapped),
                "max_spn": max(item[2] for item in mapped),
            }
        )
    output_clpi.write_bytes(data)
    return {
        "clpi": str(output_clpi),
        "scaled": True,
        "source_packets": source_packets,
        "output_packets": output_packets,
        "old_num_source_packets": old_num_source_packets,
        "new_num_source_packets": output_packets,
        "streams": stream_reports,
        "keyframes": {
            "used": any(item.get("mapping") == "actual-keyframe-spn" for item in stream_reports),
            "count": (keyframe_report or {}).get("keyframe_count"),
            "error": (keyframe_report or {}).get("error"),
        } if keyframe_report is not None else None,
    }


def splice_clpi_cpi_block(target_clpi: Path, generated_clpi: Path) -> dict[str, Any]:
    if not target_clpi.exists():
        return {"target": str(target_clpi), "exists": False, "spliced": False}
    if not generated_clpi.exists():
        return {"target": str(target_clpi), "generated": str(generated_clpi), "spliced": False, "missing_generated": True}
    target = bytearray(target_clpi.read_bytes())
    generated = generated_clpi.read_bytes()
    target_offsets = clpi_offsets(target)
    generated_offsets = clpi_offsets(generated)
    target_cpi = target_offsets["cpi"]
    target_mark = target_offsets["clip_mark"]
    target_ext = target_offsets["extension_data"]
    generated_cpi = generated_offsets["cpi"]
    generated_mark = generated_offsets["clip_mark"]
    if not (0 <= target_cpi <= target_mark <= len(target)):
        raise ToolError(f"Invalid CLPI CPI offsets in {target_clpi}")
    if not (0 <= generated_cpi <= generated_mark <= len(generated)):
        raise ToolError(f"Invalid generated CLPI CPI offsets in {generated_clpi}")
    new_cpi_block = generated[generated_cpi:generated_mark]
    old_cpi_block = bytes(target[target_cpi:target_mark])
    if old_cpi_block == new_cpi_block and target.startswith(b"HDMV0300"):
        return {
            "target": str(target_clpi),
            "generated": str(generated_clpi),
            "exists": True,
            "spliced": False,
            "reason": "cpi already current",
            "cpi_length": len(old_cpi_block),
        }
    delta = len(new_cpi_block) - len(old_cpi_block)
    target[target_cpi:target_mark] = new_cpi_block
    write_be32(target, 20, target_mark + delta)
    if target_ext:
        write_be32(target, 24, target_ext + delta)
    if target.startswith(b"HDMV0200"):
        target[4:8] = b"0300"
    target_clpi.write_bytes(target)
    return {
        "target": str(target_clpi),
        "generated": str(generated_clpi),
        "exists": True,
        "spliced": True,
        "old_cpi_length": len(old_cpi_block),
        "new_cpi_length": len(new_cpi_block),
        "delta": delta,
        "new_size": len(target),
        "new_clip_mark_offset": read_be32(target, 20),
        "new_extension_data_offset": read_be32(target, 24),
    }


def generate_clpi_for_m2ts(output_clip: Path, tools: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    tsmuxer = require_tool(tools, "tsmuxer")
    work_parent = ROOT / "work"
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{output_clip.stem}_clpi_", dir=work_parent) as tmp:
        tmp_root = Path(tmp)
        meta_path = tmp_root / f"{output_clip.stem}.clpi.meta"
        clip_info = inspect_clip(output_clip, tools, accurate_video_bitrate=False)
        write_tsmuxer_meta(output_clip, meta_path, clip_info, tools)
        run_cmd([tsmuxer, str(meta_path), str(tmp_root)], check=True, capture=not verbose, verbose=verbose)
        generated_clpi = tmp_root / "BDMV" / "CLIPINF" / "00000.clpi"
        if not generated_clpi.exists():
            raise ToolError(f"tsMuxeR did not generate CLPI for {output_clip}")
        return {
            "clip": str(output_clip),
            "generated_clpi_bytes": generated_clpi.read_bytes(),
            "generated_size": generated_clpi.stat().st_size,
        }


def refresh_clpi_cpi_from_m2ts(output_clip: Path, output_clpi: Path, tools: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    if not output_clip.exists():
        return {"clip": str(output_clip), "clpi": str(output_clpi), "refreshed": False, "missing_clip": True}
    if not output_clpi.exists():
        return {"clip": str(output_clip), "clpi": str(output_clpi), "refreshed": False, "missing_clpi": True}
    generated = generate_clpi_for_m2ts(output_clip, tools, verbose=verbose)
    with tempfile.NamedTemporaryFile(prefix=f"{output_clip.stem}_generated_", suffix=".clpi", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(generated["generated_clpi_bytes"])
    try:
        splice = splice_clpi_cpi_block(output_clpi, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {
        "clip": str(output_clip),
        "clpi": str(output_clpi),
        "refreshed": bool(splice.get("spliced")) or splice.get("reason") == "cpi already current",
        "generated_size": generated.get("generated_size"),
        "splice": splice,
    }


def patch_mpls_for_hevc(path: Path, clip_ids: set[str], *, patch_version_headers: bool = False) -> dict[str, Any]:
    if not path.exists():
        return {"file": str(path), "exists": False, "patched": False}
    data = bytearray(path.read_bytes())
    original = bytes(data)
    matched_clips: list[str] = []
    stream_patches = 0
    if data.startswith(b"MPLS") and len(data) >= 20:
        playlist_start = read_be32(data, 8)
        if 0 <= playlist_start + 10 <= len(data):
            playlist_length = read_be32(data, playlist_start)
            playlist_end = min(len(data), playlist_start + 4 + playlist_length)
            item_count = read_be16(data, playlist_start + 6)
            pos = playlist_start + 10
            for _ in range(item_count):
                if pos + 2 > playlist_end:
                    break
                item_length = read_be16(data, pos)
                item_end = min(len(data), pos + 2 + item_length)
                clip_id = data[pos + 2 : pos + 7].decode("ascii", errors="ignore") if pos + 7 <= item_end else ""
                if clip_id in clip_ids:
                    matched_clips.append(clip_id)
                    for source_pattern in (MPLS_PRIMARY_VIDEO_AVC, MPLS_PRIMARY_VIDEO_MPEG2):
                        stream_patches += replace_in_range(data, pos, item_end, source_pattern, MPLS_PRIMARY_VIDEO_HEVC)
                pos = item_end
    if patch_version_headers and stream_patches and data.startswith(b"MPLS0200"):
        data[4:8] = b"0300"
    if data != original:
        path.write_bytes(data)
    return {
        "file": str(path),
        "exists": True,
        "patched": data != original,
        "matched_clips": sorted(set(matched_clips)),
        "primary_video_patches": stream_patches,
        "version_changed": original[:8] == b"MPLS0200" and data[:8] == b"MPLS0300",
    }


def patch_navigation_for_hevc(
    disc_root: Path,
    clip_files: list[str],
    *,
    tools: dict[str, Any] | None = None,
    source_root: Path | None = None,
    refresh_cpi: bool = False,
    patch_version_headers: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    clip_ids = {Path(name).stem for name in clip_files}
    bdmv = disc_root / "BDMV"
    actual_spn_candidates = short_repeated_playitem_clips(disc_root, clip_ids)
    report: dict[str, Any] = {
        "clips": sorted(clip_ids),
        "clpi": [],
        "mpls": [],
        "actual_keyframe_spn_candidates": actual_spn_candidates,
    }
    for rel in (Path("CLIPINF"), Path("BACKUP") / "CLIPINF"):
        folder = bdmv / rel
        for clip_id in sorted(clip_ids):
            clpi_path = folder / f"{clip_id}.clpi"
            if source_root:
                source_clip = source_root / "BDMV" / "STREAM" / f"{clip_id}.m2ts"
                output_clip = bdmv / "STREAM" / f"{clip_id}.m2ts"
                item = restore_source_clpi(
                    source_clip,
                    clpi_path,
                    output_clip=output_clip,
                    patch_version_headers=patch_version_headers,
                    tools=tools,
                    prefer_actual_keyframe_spns=clip_id in actual_spn_candidates,
                )
            else:
                item = patch_clpi_for_hevc(clpi_path, patch_version_headers=patch_version_headers)
            if refresh_cpi and tools and clpi_path.exists():
                stream_path = bdmv / "STREAM" / f"{clip_id}.m2ts"
                try:
                    item["cpi_refresh"] = refresh_clpi_cpi_from_m2ts(stream_path, clpi_path, tools, verbose=verbose)
                except Exception as exc:
                    item["cpi_refresh"] = {"refreshed": False, "error": str(exc)}
            report["clpi"].append(item)
    for rel in (Path("PLAYLIST"), Path("BACKUP") / "PLAYLIST"):
        folder = bdmv / rel
        if not folder.is_dir():
            continue
        for playlist in sorted(folder.glob("*.mpls")):
            result = patch_mpls_for_hevc(playlist, clip_ids, patch_version_headers=patch_version_headers)
            if result.get("matched_clips") or result.get("patched"):
                report["mpls"].append(result)
    report["patched_clpi_count"] = sum(1 for item in report["clpi"] if item.get("patched") or (item.get("patch") or {}).get("patched"))
    report["scaled_clpi_cpi_count"] = sum(1 for item in report["clpi"] if ((item.get("cpi_scale") or {}).get("scaled")))
    report["patched_mpls_count"] = sum(1 for item in report["mpls"] if item.get("patched"))
    report["primary_video_descriptor_patches"] = (
        sum(int(item.get("primary_video_patches") or (item.get("patch") or {}).get("primary_video_patches") or 0) for item in report["clpi"])
        + sum(int(item.get("primary_video_patches") or 0) for item in report["mpls"])
    )
    return report

def source_clpi_for_stream(source_clip: Path) -> Path:
    return source_clip.parent.parent / "CLIPINF" / f"{source_clip.stem}.clpi"


def restore_source_clpi(
    source_clip: Path,
    output_clpi: Path,
    *,
    output_clip: Path | None = None,
    patch_version_headers: bool = False,
    tools: dict[str, Any] | None = None,
    prefer_actual_keyframe_spns: bool = False,
) -> dict[str, Any]:
    source_clpi = source_clpi_for_stream(source_clip)
    if not source_clpi.exists():
        return {"source_clpi": str(source_clpi), "output_clpi": str(output_clpi), "restored": False, "missing": True}
    output_clpi.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_clpi, output_clpi)
    patch_result = patch_clpi_for_hevc(output_clpi, patch_version_headers=patch_version_headers)
    cpi_scale = None
    if output_clip:
        try:
            cpi_scale = scale_clpi_cpi_map_to_stream(
                source_clip,
                output_clip,
                output_clpi,
                tools=tools,
                prefer_actual_keyframe_spns=prefer_actual_keyframe_spns,
            )
        except Exception as exc:
            cpi_scale = {"scaled": False, "error": str(exc)}
    return {
        "source_clpi": str(source_clpi),
        "output_clpi": str(output_clpi),
        "restored": True,
        "patch": patch_result,
        "cpi_scale": cpi_scale,
        "prefer_actual_keyframe_spns": prefer_actual_keyframe_spns,
    }
