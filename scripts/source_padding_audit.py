"""Audit Blu-ray backup source clips for transport and coded padding."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    import numpy as np
except Exception:  # pragma: no cover - local helper fallback
    np = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bd2hevc_app.scan import count_coded_padding_bytes, ffprobe_streams, safe_float
from bd2hevc_app.tools import ToolError, discover_tools


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit source BD backups for M2TS null packets and coded video padding.")
    parser.add_argument("root", help="Folder containing BD backup folders.")
    parser.add_argument("--report", required=True, help="JSON report path.")
    parser.add_argument("--progress", help="Progress JSON path.")
    parser.add_argument("--coded-min-size", type=int, default=0, help="Minimum M2TS byte size for coded padding scans.")
    return parser.parse_args()


def find_backup_dirs(root: Path) -> list[Path]:
    return sorted([p for p in root.iterdir() if (p / "BDMV" / "STREAM").is_dir()], key=lambda p: p.name.lower())


def detect_ts_layout(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as handle:
        data = handle.read(1024 * 1024)
    candidates = [(192, 4), (188, 0), (204, 0), (208, 4)]
    best: tuple[float, int, int] | None = None
    for packet_size, sync_offset in candidates:
        checks = 0
        matches = 0
        for start in range(0, max(0, len(data) - packet_size + 1), packet_size):
            pos = start + sync_offset
            if pos >= len(data):
                break
            checks += 1
            if data[pos] == 0x47:
                matches += 1
        if checks:
            score = matches / checks
            if best is None or score > best[0]:
                best = (score, packet_size, sync_offset)
    if best and best[0] >= 0.8:
        return best[1], best[2]
    return None


def count_null_packets_numpy(path: Path, packet_size: int, sync_offset: int) -> dict[str, int]:
    if np is None:
        return count_null_packets_python(path, packet_size, sync_offset)
    packets = 0
    bad_sync = 0
    null_packets = 0
    chunk_size = packet_size * 131072
    remainder = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            data = remainder + chunk
            full_size = (len(data) // packet_size) * packet_size
            block = data[:full_size]
            remainder = data[full_size:]
            if not block:
                continue
            arr = np.frombuffer(block, dtype=np.uint8).reshape((-1, packet_size))
            sync = arr[:, sync_offset]
            good = sync == 0x47
            packets += int(arr.shape[0])
            bad_sync += int((~good).sum())
            pid_hi = (arr[:, sync_offset + 1].astype(np.uint16) & 0x1F) << 8
            pid_lo = arr[:, sync_offset + 2].astype(np.uint16)
            pids = pid_hi | pid_lo
            null_packets += int(((pids == 0x1FFF) & good).sum())
    return {
        "packet_count": packets,
        "bad_sync_packets": bad_sync,
        "null_packet_count": null_packets,
        "null_packet_bytes": null_packets * packet_size,
    }


def count_null_packets_python(path: Path, packet_size: int, sync_offset: int) -> dict[str, int]:
    packets = 0
    bad_sync = 0
    null_packets = 0
    chunk_size = packet_size * 32768
    remainder = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            data = remainder + chunk
            full_size = (len(data) // packet_size) * packet_size
            block = data[:full_size]
            remainder = data[full_size:]
            for start in range(0, len(block), packet_size):
                sync_pos = start + sync_offset
                if block[sync_pos] != 0x47:
                    bad_sync += 1
                else:
                    pid = ((block[sync_pos + 1] & 0x1F) << 8) | block[sync_pos + 2]
                    if pid == 0x1FFF:
                        null_packets += 1
                packets += 1
    return {
        "packet_count": packets,
        "bad_sync_packets": bad_sync,
        "null_packet_count": null_packets,
        "null_packet_bytes": null_packets * packet_size,
    }


def video_stream_info(path: Path, tools: dict[str, Any]) -> dict[str, Any]:
    probe = ffprobe_streams(path, tools)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None) or {}
    fmt = probe.get("format", {})
    return {
        "duration": safe_float(fmt.get("duration")),
        "codec": str(video.get("codec_name") or "").lower() or None,
        "width": video.get("width"),
        "height": video.get("height"),
    }


def write_json(path: Path | None, data: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    args = parse_args()
    source_root = Path(args.root)
    report_path = Path(args.report)
    progress_path = Path(args.progress) if args.progress else None
    tools = discover_tools()
    backups = find_backup_dirs(source_root)
    clips_by_disc = [(disc, sorted((disc / "BDMV" / "STREAM").glob("*.m2ts"), key=lambda p: p.name)) for disc in backups]
    total_clips = sum(len(clips) for _, clips in clips_by_disc)
    started = time.time()
    report: dict[str, Any] = {
        "source_root": str(source_root),
        "started_at": started,
        "coded_min_size": args.coded_min_size,
        "discs": [],
        "summary": {},
    }
    processed = 0

    for disc, clips in clips_by_disc:
        disc_entry: dict[str, Any] = {"disc": disc.name, "clip_count": len(clips), "clips": []}
        report["discs"].append(disc_entry)
        for clip in clips:
            processed += 1
            write_json(
                progress_path,
                {
                    "status": "running",
                    "processed": processed - 1,
                    "total": total_clips,
                    "disc": disc.name,
                    "clip": clip.name,
                    "elapsed_seconds": round(time.time() - started, 1),
                },
            )
            entry: dict[str, Any] = {"file": clip.name, "size_bytes": clip.stat().st_size}
            try:
                entry.update(video_stream_info(clip, tools))
            except Exception as exc:
                entry["probe_error"] = str(exc)
            layout = detect_ts_layout(clip)
            if layout:
                packet_size, sync_offset = layout
                entry.update({"packet_size": packet_size, "sync_offset": sync_offset})
                try:
                    nulls = count_null_packets_numpy(clip, packet_size, sync_offset)
                    entry.update(nulls)
                    if entry["size_bytes"]:
                        entry["null_packet_ratio"] = round(nulls["null_packet_bytes"] / entry["size_bytes"], 6)
                except Exception as exc:
                    entry["null_packet_error"] = str(exc)
            else:
                entry["packet_layout_error"] = "could not detect TS packet layout"
            codec = str(entry.get("codec") or "").lower()
            supported_coded = codec in {"h264", "avc1", "hevc", "h265", "vc1", "wmv3"}
            entry["coded_padding_supported"] = supported_coded
            if supported_coded and entry["size_bytes"] >= args.coded_min_size:
                try:
                    coded = count_coded_padding_bytes(clip, tools, codec) or {}
                    entry.update(
                        {
                            "coded_padding_kind": coded.get("padding_kind"),
                            "coded_padding_bytes": int(coded.get("padding_bytes") or 0),
                            "coded_padding_units": int(coded.get("padding_units") or 0),
                        }
                    )
                    duration = entry.get("duration") or 0
                    if duration:
                        entry["coded_padding_bitrate_mbps"] = round(entry["coded_padding_bytes"] * 8 / duration / 1_000_000, 6)
                except ToolError as exc:
                    entry["coded_padding_error"] = str(exc)
            disc_entry["clips"].append(entry)
            write_json(report_path, report)

    total_size = 0
    total_null = 0
    total_coded = 0
    positive_coded: list[dict[str, Any]] = []
    positive_null: list[dict[str, Any]] = []
    codecs: dict[str, dict[str, Any]] = {}
    for disc in report["discs"]:
        disc_size = 0
        disc_null = 0
        disc_coded = 0
        for clip in disc["clips"]:
            size = int(clip.get("size_bytes") or 0)
            null_bytes = int(clip.get("null_packet_bytes") or 0)
            coded_bytes = int(clip.get("coded_padding_bytes") or 0)
            codec = str(clip.get("codec") or "unknown")
            codec_entry = codecs.setdefault(codec, {"clips": 0, "bytes": 0, "null_packet_bytes": 0, "coded_padding_bytes": 0})
            codec_entry["clips"] += 1
            codec_entry["bytes"] += size
            codec_entry["null_packet_bytes"] += null_bytes
            codec_entry["coded_padding_bytes"] += coded_bytes
            disc_size += size
            disc_null += null_bytes
            disc_coded += coded_bytes
            if coded_bytes:
                positive_coded.append(
                    {
                        "disc": disc["disc"],
                        "file": clip["file"],
                        "codec": clip.get("codec"),
                        "duration": clip.get("duration"),
                        "coded_padding_kind": clip.get("coded_padding_kind"),
                        "coded_padding_bytes": coded_bytes,
                        "coded_padding_bitrate_mbps": clip.get("coded_padding_bitrate_mbps"),
                    }
                )
            if null_bytes:
                positive_null.append(
                    {
                        "disc": disc["disc"],
                        "file": clip["file"],
                        "codec": clip.get("codec"),
                        "duration": clip.get("duration"),
                        "null_packet_bytes": null_bytes,
                        "null_packet_ratio": clip.get("null_packet_ratio"),
                    }
                )
        disc["size_bytes"] = disc_size
        disc["null_packet_bytes"] = disc_null
        disc["coded_padding_bytes"] = disc_coded
        total_size += disc_size
        total_null += disc_null
        total_coded += disc_coded
    report["summary"] = {
        "disc_count": len(report["discs"]),
        "clip_count": total_clips,
        "size_bytes": total_size,
        "null_packet_bytes": total_null,
        "null_packet_ratio": round(total_null / total_size, 6) if total_size else 0,
        "coded_padding_bytes": total_coded,
        "coded_padding_ratio": round(total_coded / total_size, 9) if total_size else 0,
        "positive_coded_padding_clips": positive_coded,
        "positive_null_packet_clips": positive_null,
        "by_codec": codecs,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    write_json(report_path, report)
    write_json(progress_path, {"status": "complete", "processed": total_clips, "total": total_clips, "report": str(report_path), "elapsed_seconds": round(time.time() - started, 1)})
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
