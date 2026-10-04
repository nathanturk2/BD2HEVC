"""Inspect and repair existing backups without rewriting their media payloads.

Run ``python -m bd2hevc_app.backup_repair --help`` for the standalone repair UI.
An inspection is read-only; applying always saves a durable undo journal first.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from .tools import ToolError
from .udf_repair import UdfImage, apply_patches, restore_journal
from .udf_validation import UdfValidationError, inspect_udf, validate_udf
from .video_navigation import clpi_video_entries, mpls_video_entries, probe_video, reconcile_clpi, reconcile_mpls


def plan_backup(path: Path, tools: dict, *, filesystem_only=False, assume_bd_sdr=False) -> dict:
    path = path.resolve()
    stat = path.stat()
    result = {"schema": "bd2hevc-backup-repair-plan-v1", "path": str(path),
              "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "files": [],
              "issues": [], "warnings": [], "video": {}, "metadata_changes": [], "source_streams": []}
    image = None
    if path.is_file():
        volume = inspect_udf(path)
        if len(volume["issues"]) != len(volume["patches"]):
            raise UdfValidationError("ISO has defects outside the supported repair")
        result["files"].append({"path": str(path), "size": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns, "patches": volume["patches"]})
        result["filesystem_issues"] = volume["issues"]
        if filesystem_only:
            return result
        image = UdfImage(path)
        names = image.files
        read = image.read
    else:
        if filesystem_only:
            return result
        if not (path / "BDMV").is_dir():
            raise ToolError(f"Not a Blu-ray backup: {path}")
        names = {p.relative_to(path).as_posix(): None for p in (path / "BDMV").rglob("*") if p.is_file()}
        def read(name, limit=None):
            with (path / name).open("rb") as handle:
                return handle.read(-1 if limit is None else limit)

    metadata = {name: read(name) for name in names
                if name.endswith((".clpi", ".mpls")) and name.startswith((
                    "BDMV/CLIPINF/", "BDMV/PLAYLIST/", "BDMV/BACKUP/CLIPINF/", "BDMV/BACKUP/PLAYLIST/"))}
    clip_ids = {Path(name).stem for name in metadata if name.endswith(".clpi")}
    with tempfile.TemporaryDirectory(prefix="bd2hevc-metadata-") as temporary:
        sample = Path(temporary) / "sample.m2ts"
        for clip in sorted(clip_ids):
            stream = f"BDMV/STREAM/{clip}.m2ts"
            if stream not in names:
                result["issues"].append(f"Missing stream: {stream}")
                continue
            entries = []
            for name in (f"BDMV/CLIPINF/{clip}.clpi", f"BDMV/BACKUP/CLIPINF/{clip}.clpi"):
                if name in metadata:
                    entries.extend(clpi_video_entries(metadata[name]))
            if not entries:
                continue
            try:
                if image:
                    sample.write_bytes(read(stream, 8 * 1024 * 1024))
                    video = probe_video(sample, tools, assume_bd_sdr=assume_bd_sdr, hevc_only=True)
                else:
                    stream_stat = (path / stream).stat()
                    video = probe_video(path / stream, tools, assume_bd_sdr=assume_bd_sdr, hevc_only=True)
                    after_stat = (path / stream).stat()
                    if (stream_stat.st_size, stream_stat.st_mtime_ns) != (after_stat.st_size, after_stat.st_mtime_ns):
                        raise ToolError(f"Stream changed during inspection: {stream}")
                    result["source_streams"].append({"path": str(path / stream), "size": stream_stat.st_size,
                                                     "mtime_ns": stream_stat.st_mtime_ns})
                # Only converted HEVC video needs new navigation attributes.
                # Copied source streams retain their original authoring metadata.
                result["video"][clip] = {pid: props for pid, props in video.items() if props["codec"] == 0x24}
                for props in result["video"][clip].values():
                    if props["inferred_bd709"]:
                        result["warnings"].append(f"{clip}: BT.709 SDR inferred from an 8-bit HD BD conversion; stream VUI is unspecified")
                    elif props["color_space"] is None:
                        result["warnings"].append(f"{clip}: unspecified stream colour primaries; navigation colour left unchanged")
            except (ToolError, ValueError) as exc:
                result["issues"].append(f"{clip}: {exc}")

    for name, original in sorted(metadata.items()):
        data = bytearray(original)
        if name.endswith(".clpi"):
            changes = reconcile_clpi(data, result["video"].get(Path(name).stem, {}))
        else:
            for clip, _, _, _ in mpls_video_entries(data):
                if f"BDMV/STREAM/{clip}.m2ts" not in names:
                    issue = f"{name}: missing referenced stream {clip}"
                    if issue not in result["issues"]:
                        result["issues"].append(issue)
            changes = reconcile_mpls(data, result["video"])
        if not changes:
            continue
        result["metadata_changes"].append({"file": name, "descriptors": changes})
        if image:
            result["files"][0]["patches"].extend(image.replacement(name, bytes(data)))
        else:
            target = path / name
            st = target.stat()
            result["files"].append({"path": str(target), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                    "patches": [{"offset": 0, "before": original.hex(), "after": data.hex(), "reason": name}]})
    if image and (path.stat().st_size, path.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise UdfValidationError("Backup changed while inspecting; retry when conversion has stopped")
    return result


def apply_plan(plan: dict, journal_dir: Path) -> list[str]:
    if plan.get("schema") != "bd2hevc-backup-repair-plan-v1":
        raise UdfValidationError("Unsupported repair plan")
    root = Path(plan["path"]).resolve()
    for stream in plan.get("source_streams", []):
        st = Path(stream["path"]).stat()
        if (st.st_size, st.st_mtime_ns) != (stream["size"], stream["mtime_ns"]):
            raise UdfValidationError("Video changed since inspection; rescan before repairing navigation")
    journals = []
    for item in plan["files"]:
        target = Path(item["path"]).resolve()
        if target != root and root not in target.parents:
            raise UdfValidationError("Repair target is outside the selected backup")
        journal = apply_patches(target, item["patches"], journal_dir,
                                expected_stat=(item["size"], item["mtime_ns"]))
        if journal:
            journals.append(str(journal))
    if root.is_file():
        validate_udf(root)
    return journals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Converted ISO or Blu-ray folder; with --restore, an undo journal")
    parser.add_argument("--apply", action="store_true", help="Apply verified changes with undo journals (default: inspect only)")
    parser.add_argument("--restore", action="store_true", help="Restore original bytes from a saved repair journal")
    parser.add_argument("--filesystem-only", action="store_true", help="Check/repair UDF descriptors only; also suitable for DVD ISOs")
    parser.add_argument("--assume-bd-sdr", action="store_true", help="Use BT.709 SDR for untagged 8-bit HD BD conversions")
    parser.add_argument("--ffprobe", default=str(Path(__file__).resolve().parents[1] / "tools/ffmpeg/bin/ffprobe.exe"))
    parser.add_argument("--journal-dir", type=Path, default=Path("backup-repair-journals"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.restore:
            if args.apply:
                parser.error("--restore and --apply are mutually exclusive")
            restore_journal(args.path)
            return
        if args.report:
            report_path, backup_path = args.report.resolve(), args.path.resolve()
            if report_path == backup_path or backup_path in report_path.parents:
                parser.error("Keep the repair report outside the backup being repaired")
            args.report.parent.mkdir(parents=True, exist_ok=True)
        plan = plan_backup(args.path, {"ffprobe": args.ffprobe}, filesystem_only=args.filesystem_only,
                           assume_bd_sdr=args.assume_bd_sdr)
        if args.apply:
            plan["journals"] = apply_plan(plan, args.journal_dir)
        summary = {key: value for key, value in plan.items() if key not in ("files", "video", "source_streams")}
        summary["patch_count"] = sum(len(item["patches"]) for item in plan["files"])
        if args.report:
            args.report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    except (OSError, ValueError, ToolError) as exc:
        parser.exit(2, f"Repair failed: {exc}\n")


if __name__ == "__main__":
    main()
