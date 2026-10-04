from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bd2hevc_app import core, encoding, iso, output, queueing, scan, tools
from bd2hevc_app.tools import ToolError


class OutputSafetyTests(unittest.TestCase):
    def test_queueing_forced_conversion_keeps_previous_output(self) -> None:
        for output_format in ("iso", "folder"):
            with self.subTest(output_format=output_format), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "BDMV").mkdir(parents=True)
                destination = root / ("movie.iso" if output_format == "iso" else "movie")
                marker = destination
                if output_format == "folder":
                    destination.mkdir()
                    marker = destination / "keep.m2ts"
                marker.write_bytes(b"previous conversion")
                args = core.build_parser().parse_args([
                    "start", str(source), str(destination), "--force", "--output-format", output_format,
                ])
                with patch.object(core, "discover_tools", return_value={
                    "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "tsmuxer": "tsmuxer",
                }), patch.object(core, "require_working_hevc_encoder"), patch.object(
                    queueing, "DEFAULT_JOB_DIR", root / "jobs"
                ), patch.object(core, "start_background_process", return_value=123):
                    job = core.enqueue_conversion_job(args, announce=False)
                self.assertEqual(marker.read_bytes(), b"previous conversion")
                self.assertTrue(job["output_existed_at_queue"])
                self.assertFalse(job["output_created_by_job"])
                self.assertIn("--force", job["command"])

    def test_force_rejects_output_containing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "library"
            source = parent / "original"
            source.mkdir(parents=True)
            marker = source / "keep.m2ts"
            marker.write_bytes(b"original")
            with self.assertRaises(ToolError):
                output.make_output_available(parent, source, force=True)
            self.assertEqual(marker.read_bytes(), b"original")

    def test_disk_full_cleanup_rejects_overlapping_source_paths(self) -> None:
        for target_kind in ("ancestor", "descendant", "staging-ancestor"):
            with self.subTest(target_kind=target_kind), tempfile.TemporaryDirectory() as temporary:
                parent = Path(temporary) / "library"
                source = parent / "original"
                source.mkdir(parents=True)
                marker = source / "keep.m2ts"
                marker.write_bytes(b"original")
                job = {"source": str(source), "output_created_by_job": True}
                if target_kind == "ancestor":
                    job["output"] = str(parent)
                elif target_kind == "descendant":
                    job["output"] = str(marker)
                else:
                    job["output"] = str(Path(temporary) / "new.iso")
                    job["staging_output"] = str(parent)
                removed, _reason = queueing.remove_new_partial_output(job)
                self.assertFalse(removed)
                self.assertEqual(marker.read_bytes(), b"original")


class AudioDryRunTests(unittest.TestCase):
    def test_dry_run_does_not_create_directories_or_remove_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in (root, root / "new"):
                existing = directory / "00001.audio00.ac3"
                if directory.exists():
                    existing.write_bytes(b"keep audio")
                with patch.object(encoding, "run_cmd") as run:
                    encoding.transcode_compact_audio_tracks(
                        root / "input.m2ts", directory / "00001.compact-audio",
                        {"audio": [{"index": 1, "channels": 2}]},
                        {"ffmpeg": "ffmpeg"}, dry_run=True,
                    )
                run.assert_not_called()
            self.assertEqual((root / "00001.audio00.ac3").read_bytes(), b"keep audio")
            self.assertFalse((root / "new").exists())

    def test_compact_m2ts_maps_only_playable_audio_tracks(self) -> None:
        command = encoding.encode_to_hevc_m2ts(
            Path("input.m2ts"), Path("output.m2ts"),
            {"video": {"fps": 24, "target_hevc": {"target_bps": 2_000_000}},
             "audio": [{"index": 1, "channels": 0}, {"index": 2, "channels": 1}]},
            {"ffmpeg": "ffmpeg"}, audio_mode="compact-stereo", dry_run=True,
        )
        maps = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
        self.assertEqual(maps, ["0:v:0", "0:2"])
        self.assertEqual(command[command.index("-ac:a:0") + 1], "1")


class IsoPublicationTests(unittest.TestCase):
    def test_force_conversion_passes_replacement_to_author_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            destination = root / "movie.iso"
            destination.write_bytes(b"previous verified image")
            args = argparse.Namespace(
                source=str(source), output=str(destination), output_format="iso",
                force=True, dry_run=False, verbose=False, keep_iso_staging=True,
            )

            def publish(_source: Path, target: Path, **kwargs: object) -> dict:
                self.assertTrue(kwargs["force"])
                self.assertEqual(target.read_bytes(), b"previous verified image")
                target.write_bytes(b"replacement")
                return {"ok": True}

            with patch.object(core, "convert_clone_streams", return_value={
                "mode": "clone-streams", "validation": [{"ok": True}],
            }), patch.object(core, "author_bluray_iso", side_effect=publish), patch.object(core, "progress_event"):
                result = core.convert_clone_streams_with_output_format(args, {})
            self.assertTrue(result["iso_authoring"]["ok"])
            self.assertEqual(destination.read_bytes(), b"replacement")

    def test_force_conversion_keeps_existing_iso_if_conversion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            destination = root / "movie.iso"
            destination.write_bytes(b"previous verified image")
            args = argparse.Namespace(
                source=str(source), output=str(destination), output_format="iso",
                force=True, dry_run=False,
            )
            with patch.object(core, "convert_clone_streams", side_effect=ToolError("encode failed")):
                with self.assertRaisesRegex(ToolError, "encode failed"):
                    core.convert_clone_streams_with_output_format(args, {})
            self.assertEqual(destination.read_bytes(), b"previous verified image")

    def test_force_authoring_preserves_existing_iso_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            destination = root / "movie.iso"
            destination.write_bytes(b"previous verified image")
            partial = iso.iso_partial_path(destination)

            def create(_tool: Path, _args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                partial.write_bytes(b"new verified image")
                return subprocess.CompletedProcess([], 0, "Found 1 files")

            with patch.object(iso, "validate_bluray_folder"), patch.object(
                iso, "folder_stats", return_value=(1, 0, 3)
            ), patch.object(iso, "find_udf_tool", return_value=Path("udf")), patch.object(
                iso, "_run", side_effect=create
            ), patch.object(iso, "verify_bluray_iso", return_value={"ok": True}), patch.object(iso, "verify_payload", return_value={"schema":"bd2hevc-payload-sha256-v1", "files":[]}), patch.object(
                iso.os, "replace", side_effect=PermissionError("locked")
            ):
                with self.assertRaises(PermissionError):
                    iso.author_bluray_iso(source, destination, force=True)
            self.assertEqual(destination.read_bytes(), b"previous verified image")
            self.assertFalse(partial.exists())


class StreamingScanTests(unittest.TestCase):
    def test_padding_scans_drain_large_stderr_without_stalling(self) -> None:
        real_popen = subprocess.Popen
        for scanner, payload, expected in (
            (scan.count_h264_filler_bytes, b"\x00\x00\x01\x0c\xff\x80", 6),
            (scan.count_vc1_stuffing_bytes, b"\x00\x00\x00\x00\x01\x0f", 2),
        ):
            with self.subTest(scanner=scanner.__name__):
                expired = threading.Event()
                timers = []

                def start(_command: list[str], **kwargs: object) -> subprocess.Popen:
                    program = (
                        "import sys; sys.stderr.buffer.write(b'x' * 524288); sys.stderr.flush(); "
                        f"sys.stdout.buffer.write({payload!r})"
                    )
                    process = real_popen([sys.executable, "-c", program], **kwargs)

                    def timeout() -> None:
                        expired.set()
                        process.kill()

                    timer = threading.Timer(5, timeout)
                    timer.start()
                    timers.append(timer)
                    return process

                try:
                    with patch.object(tools.subprocess, "Popen", side_effect=start):
                        result = scanner(Path("synthetic.m2ts"), {"ffmpeg": "ffmpeg"})
                    self.assertFalse(expired.is_set(), "Scanner blocked on the child's error pipe")
                    self.assertEqual(result["padding_bytes"], expected)
                finally:
                    for timer in timers:
                        timer.cancel()
                        timer.join()

    def test_streaming_command_retains_a_bounded_error_tail(self) -> None:
        result = tools.run_streaming_cmd(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 100000 + 'failure detail'); sys.exit(7)"],
            lambda _chunk: None,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(result.stderr), 64 * 1024)
        self.assertTrue(result.stderr.endswith(b"failure detail"))


if __name__ == "__main__":
    unittest.main()
