"""Audio roles must survive passthrough authoring without weakening validation."""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bd2hevc_app import core, muxing, validation
from bd2hevc_app.tools import ToolError


class AudioRoleMetadataTests(unittest.TestCase):
    def test_probe_retains_secondary_role_independently_of_codec_and_pid(self):
        probe = """Track ID: 2
Stream type: E-AC3 (DD+)
Secondary: 1
Stream ID: A_AC3
Stream lang: eng
Track ID: 4352
Stream type: E-AC3 (DD+)
Secondary: 0
Stream ID: A_AC3
Track ID: 6657
Stream type: DTS-HD
Secondary: 1
Stream ID: A_DTS
"""
        with patch.object(muxing, "run_cmd", return_value=subprocess.CompletedProcess([], 0, probe, "")):
            tracks = muxing.parse_tsmuxer_tracks(Path("source.m2ts"), {"tsmuxer": "tsmuxer"})
        self.assertEqual([t["secondary"] for t in tracks], ["1", "0", "1"])
        self.assertEqual([t["track"] for t in tracks], ["2", "4352", "6657"])

    def write_meta(self, writer, root, tracks, **kwargs):
        meta = root / (writer + ".meta")
        clip = {"video": {"fps": 24.0, "width": 1920, "height": 1080}}
        with patch.object(muxing, "parse_tsmuxer_tracks", return_value=tracks):
            if writer == "single":
                muxing.write_tsmuxer_meta(root / "source.m2ts", meta, clip, {"tsmuxer": "tsmuxer"})
            elif writer == "split":
                muxing.write_tsmuxer_split_meta(root / "video.hevc", root / "source.m2ts", meta, clip,
                                                {"tsmuxer": "tsmuxer"}, **kwargs)
            else:
                muxing.write_tsmuxer_m2ts_split_meta(root / "video.hevc", root / "source.m2ts", meta, clip,
                                                     {"tsmuxer": "tsmuxer"}, **kwargs)
        return meta.read_text(encoding="utf-8").splitlines()

    def test_all_passthrough_writers_keep_roles_for_mixed_codecs_and_orders(self):
        # Primary DD+ and secondary DD+ share A_AC3. TrueHD's embedded AC3
        # remains one tsMuxeR input track. Role comes from the probe, not codec.
        audio = [
            {"track": "4352", "stream_id": "A_AC3", "stream_type": "AC3", "secondary": "0"},
            {"track": "4353", "stream_id": "A_AC3", "stream_type": "TRUE-HD", "stream_lang": "eng"},
            {"track": "4354", "stream_id": "A_AC3", "stream_type": "E-AC3 (DD+)", "secondary": "0"},
            {"track": "6656", "stream_id": "A_AC3", "stream_type": "E-AC3 (DD+)", "secondary": "1", "stream_lang": "jpn"},
            {"track": "6657", "stream_id": "A_DTS", "stream_type": "DTS-HD", "secondary": "1"},
            {"track": "4355", "stream_id": "A_DTS", "stream_type": "DTS-HD", "secondary": "0"},
            {"track": "4356", "stream_id": "A_LPCM"},
        ]
        orders = [audio, audio[3:5] + audio[:3] + audio[5:], [audio[i] for i in (0, 3, 1, 4, 2, 5, 6)]]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for writer in ("single", "split", "m2ts"):
                for order_index, order in enumerate(orders):
                    with self.subTest(writer=writer, order=order_index):
                        tracks = [{"track": "4113", "stream_id": "V_MPEGH/ISO/HEVC"}, *order,
                                  {"track": "4608", "stream_id": "S_HDMV/PGS", "secondary": "1"}]
                        lines = self.write_meta(writer, root, tracks)
                        emitted = [line for line in lines if line.startswith("A_")]
                        self.assertEqual(len(emitted), len(order))
                        for track, line in zip(order, emitted):
                            options = line.split(", ")[2:]
                            self.assertIn("track=" + track["track"], options)
                            self.assertEqual("secondary" in options, track.get("secondary") == "1")
                            if track.get("stream_lang"):
                                self.assertIn("lang=" + track["stream_lang"], options)
                        self.assertNotIn("secondary", next(line for line in lines if line.startswith("S_")))

    def test_secondary_role_does_not_depend_on_standard_bluray_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            for writer in ("single", "split", "m2ts"):
                with self.subTest(writer=writer):
                    lines = self.write_meta(writer, Path(temp), [{"track": "2", "stream_id": "A_DTS", "secondary": "1"}])
                    self.assertIn("track=2, secondary", next(line for line in lines if line.startswith("A_")))

    def test_secondary_input_can_be_omitted_without_changing_other_tracks(self):
        with tempfile.TemporaryDirectory() as temp:
            lines = self.write_meta("split", Path(temp), [
                {"track": "6656", "stream_id": "A_AC3", "secondary": "1"},
                {"track": "4608", "stream_id": "S_HDMV/PGS"},
            ], include_audio=False)
            self.assertFalse(any(line.startswith("A_") for line in lines))
            self.assertTrue(any(line.startswith("S_") for line in lines))

    def test_separate_audio_input_uses_its_own_reported_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = root / "audio.meta"
            with patch.object(muxing, "parse_tsmuxer_tracks", side_effect=[
                [{"track": "4352", "stream_id": "A_AC3", "secondary": "0"}],
                [{"track": "2", "stream_id": "A_DTS", "secondary": "1", "stream_lang": "fra"}],
            ]):
                muxing.write_tsmuxer_m2ts_split_meta(root / "video.hevc", root / "source.m2ts", meta,
                    {"video": {}}, {"tsmuxer": "tsmuxer"}, audio_tracks_input=root / "audio.m2ts")
            audio_line = next(line for line in meta.read_text().splitlines() if line.startswith("A_"))
            self.assertIn('audio.m2ts"', audio_line)
            self.assertIn("track=2, lang=fra, secondary", audio_line)

    def test_compact_audio_does_not_inherit_passthrough_secondary_option(self):
        with tempfile.TemporaryDirectory() as temp:
            lines = self.write_meta("m2ts", Path(temp), [{"track": "6656", "stream_id": "A_AC3", "secondary": "1"}],
                compact_audio_tracks=[{"path": "audio.ac3", "language": "eng"}])
            audio = [line for line in lines if line.startswith("A_")]
            self.assertEqual(len(audio), 1)
            self.assertIn("lang=eng", audio[0])
            self.assertNotIn("secondary", audio[0])
            self.assertNotIn("track=", audio[0])


class AudioFailureDiagnosticsTests(unittest.TestCase):
    def test_pid_mismatch_remains_fatal_and_reports_compared_identities(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "source.m2ts", Path(temp) / "output.m2ts"
            source.touch()
            output.touch()
            reference = {"ok": True, "duration": 20, "video": {"codec_name": "hevc"}, "subtitles": [],
                         "audio": [{"codec_name": "eac3", "id": "0x1a01", "channels": 2, "sample_rate": "48000"}]}
            converted = {**reference, "audio": [{**reference["audio"][0], "id": "0x1100"}]}
            with patch.object(validation, "inspect_clip", side_effect=[converted, reference]):
                result = validation.validate_clip(source, output, {}, require_hevc="always")
            self.assertFalse(result["ok"])
            check = next(c for c in result["checks"] if c["name"] == "audio_identity_preserved")
            self.assertFalse(check["ok"])
            self.assertEqual(check["source"][0]["id"], "0x1a01")
            self.assertEqual(check["output"][0]["id"], "0x1100")
            self.assertEqual(result["source_probe"], reference)

    def test_failed_results_survive_cleanup_and_preserve_prior_attempts_and_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, destination, reports = root / "source", root / "output", root / "reports"
            source.mkdir()
            destination.mkdir()
            marker = destination / "keep.m2ts"
            marker.write_bytes(b"existing output")
            reference = source / "keep.m2ts"
            reference.write_bytes(b"existing source")
            captured = []
            stages = []
            def convert(args, _tools):
                stage = Path(args.output)
                stage.mkdir()
                stages.append(stage)
                (stage / "failed.m2ts").write_bytes(b"failed replacement")
                result = {"mode": "clone-streams", "source": str(source), "output": str(stage),
                          "validation": [{"output": str(stage / "failed.m2ts"), "ok": False,
                              "checks": [{"name": "audio_identity_preserved", "ok": False,
                                          "source": [{"id": "0x1a00"}], "output": [{"id": "0x110b"}]}]}]}
                captured.append(result)
                return result
            args = argparse.Namespace(source=str(source), output=str(destination), force=True, dry_run=False)
            with patch.object(core, "DEFAULT_REPORT_DIR", reports), patch.object(core, "_convert_clone_streams_in_place", side_effect=convert):
                for _ in range(2):
                    with self.assertRaisesRegex(ToolError, "Full validation report:"):
                        core.convert_clone_streams(args, {})
            saved = [json.loads(path.read_text()) for path in (reports / "validation-failures").glob("*.json")]
            self.assertCountEqual(saved, captured)
            self.assertTrue(all(not stage.exists() for stage in stages))
            self.assertEqual(marker.read_bytes(), b"existing output")
            self.assertEqual(reference.read_bytes(), b"existing source")


if __name__ == "__main__":
    unittest.main()
