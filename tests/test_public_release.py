from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bd2hevc_app import core, diagnostics, iso, queueing, validation
from bd2hevc_app.locking import FileLock
from bd2hevc_app.repair_transaction import publish_repair
from bd2hevc_app.tools import ToolError


class PublicReleaseTests(unittest.TestCase):
    def test_two_producers_cannot_reserve_the_same_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source" / "BDMV").mkdir(parents=True)
            script = '''
import sys
from unittest.mock import patch
from bd2hevc_app import core
from bd2hevc_app.tools import ToolError
args=core.build_parser().parse_args(["start",sys.argv[1],sys.argv[2],"--name",sys.argv[3]])
with patch.object(core,"discover_tools",return_value={"ffmpeg":"ffmpeg","ffprobe":"ffprobe","tsmuxer":"tsmuxer"}), patch.object(core,"require_working_hevc_encoder"), patch.object(core,"start_background_process",return_value=0):
    try:
        core.enqueue_conversion_job(args,announce=False)
        print("admitted")
    except ToolError as error:
        if "reserved" not in str(error): raise
        print("reserved")
'''
            environment = {**os.environ, "BD2HEVC_STATE_DIR": str(root / "state")}
            children = [subprocess.Popen([sys.executable, "-c", script, str(root / "source"), str(root / "output"), name],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("one", "two")]
            results = [child.communicate(timeout=20) for child in children]
            self.assertEqual([child.returncode for child in children], [0, 0], results)
            self.assertEqual(sorted(stdout.strip() for stdout, _ in results), ["admitted", "reserved"])

    def test_swapped_audio_languages_fail_even_when_codecs_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "in.m2ts", Path(temporary) / "out.m2ts"
            source.touch(); output.touch()
            original = {"ok": True, "duration": 20, "video": {"codec_name": "h264"}, "subtitles": [],
                "audio": [{"codec_name": "ac3", "language": language, "channels": 2} for language in ("eng", "fra")]}
            converted = {**original, "video": {"codec_name": "hevc"}, "audio": list(reversed(original["audio"]))}
            with patch.object(validation, "inspect_clip", side_effect=[converted, original]):
                result = validation.validate_clip(source, output, {}, require_hevc="always")
            self.assertFalse(result["ok"])

    def test_planned_scaling_and_audio_omission_still_validate_subtitles(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "in.m2ts", Path(temporary) / "out.m2ts"
            source.touch(); output.touch()
            original = {"ok": True, "duration": 20, "video": {"codec_name": "h264", "width": 1920, "height": 1080},
                "audio": [{"codec_name": "ac3"}], "subtitles": [{"codec_name": "hdmv_pgs_subtitle", "language": "eng"}]}
            converted = {**original, "video": {"codec_name": "hevc", "width": 3840, "height": 2160}, "audio": []}
            with patch.object(validation, "inspect_clip", side_effect=[converted, original]):
                result = validation.validate_clip(source, output, {}, require_hevc="always", preserve_audio=False,
                    expected_video={"width": 3840, "height": 2160})
            self.assertTrue(result["ok"], result)
            with patch.object(validation, "inspect_clip", side_effect=[{**converted, "subtitles": []}, original]):
                result = validation.validate_clip(source, output, {}, require_hevc="always", preserve_audio=False,
                    expected_video={"width": 3840, "height": 2160})
            self.assertFalse(result["ok"])

    def test_movie_staging_cleanup_preserves_existing_user_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, staging = root / "source", root / "staging"
            source.mkdir(); staging.mkdir()
            clip = source / "clip.m2ts"; clip.touch()
            marker = staging / "keep.txt"; marker.write_text("user file")
            args = core.build_parser().parse_args(["convert", str(source), str(root / "output"), "--mode", "movie-only", "--staging-dir", str(staging)])
            with patch.object(core, "run_makemkv_scan", return_value={}), patch.object(core, "choose_title", return_value={"id": 0}), patch.object(core, "clip_path_for_title", return_value=clip), patch.object(core, "inspect_clip", side_effect=ToolError("probe failed")):
                with self.assertRaises(ToolError): core.convert_movie_only(args, {})
            self.assertEqual(marker.read_text(), "user file")

    def test_diagnostic_output_cannot_replace_media_even_with_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.iso"
            source.write_bytes(b"original")
            with patch.object(diagnostics, "discover_tools", return_value={}):
                with self.assertRaises(ToolError):
                    diagnostics.create_diagnostic_bundle(source, output=source, run_validation=False, force=True)
            self.assertEqual(source.read_bytes(), b"original")

    def test_diagnostic_directory_cannot_replace_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "backup"
            source.mkdir()
            marker = source / "keep.txt"
            marker.write_text("original")
            with patch.object(diagnostics, "discover_tools", return_value={}):
                with self.assertRaises(ToolError):
                    diagnostics.create_diagnostic_bundle(source, output=source, run_validation=False, zip_output=False, force=True)
            self.assertEqual(marker.read_text(), "original")

    def test_json_escaped_windows_path_is_redacted(self):
        source = Path(r"C:\PrivateCollection\Movie.iso")
        serialized = json.dumps({"source": str(source)})
        self.assertNotIn("PrivateCollection", diagnostics.redact_text(serialized, diagnostics.redaction_map([source])))

    def test_failed_forced_folder_replacement_preserves_previous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source", root / "output"
            source.mkdir(); output.mkdir()
            (output / "keep").write_bytes(b"previous")
            args = argparse.Namespace(source=str(source), output=str(output), force=True, dry_run=False)
            with patch.object(core, "_convert_clone_streams_in_place", side_effect=ToolError("scan failed")):
                with self.assertRaises(ToolError): core.convert_clone_streams(args, {})
            self.assertEqual((output / "keep").read_bytes(), b"previous")

    def test_missing_and_empty_validation_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "BDMV" / "STREAM").mkdir(parents=True)
            for target in (root / "missing.m2ts", root):
                args = core.build_parser().parse_args(["validate", str(target), "--no-makemkv"])
                with patch.object(core, "discover_tools", return_value={}):
                    with self.assertRaises(ToolError): core.cmd_validate(args)

    def test_missing_subtitle_is_a_preservation_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "in.m2ts", Path(temporary) / "out.m2ts"
            source.touch(); output.touch()
            original = {"ok":True, "duration":20, "video":{"codec_name":"h264"}, "audio":[], "subtitles":[{"codec_name":"hdmv_pgs_subtitle", "id":"0x1200"}]}
            converted = {**original, "video":{"codec_name":"hevc"}, "subtitles":[]}
            with patch.object(validation, "inspect_clip", side_effect=[converted, original]):
                result = validation.validate_clip(source, output, {}, require_hevc="always")
            self.assertFalse(result["ok"])

    def test_spawn_does_not_overwrite_completed_worker_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source" / "BDMV").mkdir(parents=True)
            args = core.build_parser().parse_args(["start", str(root / "source"), str(root / "out")])
            def spawn(path):
                job = queueing.load_job(path)
                job.update(status="completed", worker_marker=True)
                queueing.save_job(path, job)
                return 123
            with patch.object(core, "discover_tools", return_value={"ffmpeg":"ffmpeg", "ffprobe":"ffprobe", "tsmuxer":"tsmuxer"}), patch.object(core, "require_working_hevc_encoder"), patch.object(queueing, "DEFAULT_JOB_DIR", root / "jobs"), patch.object(core, "start_background_process", side_effect=spawn):
                result = core.enqueue_conversion_job(args, announce=False)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["worker_marker"])

    def test_work_slot_excludes_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "work.lock"
            command = [sys.executable, "-c", "from pathlib import Path; from bd2hevc_app.locking import FileLock; lock=FileLock(Path("+repr(str(path))+"), timeout=0); print(lock.acquire()); lock.release()"]
            with FileLock(path):
                self.assertEqual(subprocess.check_output(command, text=True).strip(), "False")
            self.assertEqual(subprocess.check_output(command, text=True).strip(), "True")

    def test_cancel_is_not_lost_by_stale_state_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.json"
            queueing.save_job(path, {"status":"canceled", "cancel_requested":True})
            queueing.save_job(path, {"status":"running"})
            self.assertEqual(queueing.load_job(path)["status"], "canceled")

    def test_repair_rolls_back_both_files_when_metadata_publication_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, metadata = root / "clip.m2ts", root / "clip.clpi"
            staged, staged_metadata = root / "new.m2ts", root / "new.clpi"
            for path, value in [(output,b"old video"),(metadata,b"old metadata"),(staged,b"new video"),(staged_metadata,b"new metadata")]: path.write_bytes(value)
            replace = os.replace
            def fail_metadata(source, destination):
                if Path(source) == staged_metadata: raise OSError("publication failure")
                return replace(source, destination)
            with patch("bd2hevc_app.repair_transaction.os.replace", side_effect=fail_metadata):
                with self.assertRaises(OSError): publish_repair(staged, staged_metadata, output, metadata)
            self.assertEqual(output.read_bytes(), b"old video")
            self.assertEqual(metadata.read_bytes(), b"old metadata")

    @unittest.skipUnless(os.name == "nt" or os.environ.get("BD2HEVC_UDF_TOOL"), "requires a native Hadris writer")
    def test_real_iso_equal_length_payload_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source"
            (source / "BDMV" / "PLAYLIST").mkdir(parents=True)
            (source / "BDMV" / "STREAM").mkdir()
            for name in ["index.bdmv", "MovieObject.bdmv"]: (source / "BDMV" / name).write_bytes(b"metadata")
            (source / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"synthetic payload" * 100)
            try: tool = iso.find_udf_tool()
            except ToolError: self.skipTest("native Hadris writer unavailable")
            image = root / "disc.iso"
            report = iso.author_bluray_iso(source, image, tool=tool)
            self.assertTrue(report["payload_manifest"]["verified_against_reference"])
            from bd2hevc_app.udf_repair import UdfImage
            offset = UdfImage(image).files["BDMV/STREAM/00000.m2ts"]["extents"][0][0]
            with image.open("r+b") as handle: handle.seek(offset); handle.write(b"broken")
            with self.assertRaises(ToolError): iso.verify_bluray_iso(image, tool=tool, manifest=report["manifest_path"])
