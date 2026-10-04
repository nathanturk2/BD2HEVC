import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bd2hevc_app.tools import ToolError
from bd2hevc_app.udf_validation import BLOCK, UdfValidationError, inspect_udf, update_tag, validate_udf
from bd2hevc_app.udf_repair import UdfImage, apply_patches, restore_journal
from bd2hevc_app.video_navigation import clpi_video_entries, mpls_video_entries, reconcile_clpi, reconcile_mpls, stream_descriptor
from navigation_fixtures import clpi, mpls
from bd2hevc_app import navigation, encoding


class VideoNavigationTests(unittest.TestCase):
    def test_full_navigation_refresh_probes_once_and_updates_both_copies(self):
        props = {0x1011: {"codec": 0x24, "format": 6, "rate": 4, "color_space": 1, "dynamic_range": 0}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for prefix in ("BDMV", "BDMV/BACKUP"):
                (root / prefix / "CLIPINF").mkdir(parents=True)
                (root / prefix / "PLAYLIST").mkdir()
                (root / prefix / "CLIPINF/00001.clpi").write_bytes(clpi(0xEA, 0x44))
                (root / prefix / "PLAYLIST/00001.mpls").write_bytes(mpls(0xEA, 0x44))
            with mock.patch.object(navigation, "probe_video", return_value=props) as probe:
                report = navigation.patch_navigation_for_hevc(root, ["00001.m2ts"], tools={"ffprobe": "ffprobe"})
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(report["primary_video_descriptor_patches"], 4)
            self.assertEqual((root / "BDMV/CLIPINF/00001.clpi").read_bytes(),
                             (root / "BDMV/BACKUP/CLIPINF/00001.clpi").read_bytes())

    def test_encoder_preserves_measured_colour(self):
        video = {"fps": 24, "target_hevc": {"target_bps": 1000000}, "color_primaries": "bt709",
                 "color_transfer": "bt709", "color_space": "bt709", "color_range": "tv"}
        cmd = encoding.encode_to_hevc_m2ts(Path("in.m2ts"), Path("out.m2ts"), {"video": video},
                                           {"ffmpeg": "ffmpeg"}, dry_run=True)
        for flag, value in (("-color_primaries:v:0", "bt709"), ("-color_trc:v:0", "bt709"),
                            ("-colorspace:v:0", "bt709"), ("-color_range:v:0", "tv")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)

    def test_reserved_frame_rate_codes_are_never_generated(self):
        props = stream_descriptor({"id": "0x1011", "codec_name": "hevc", "width": 1920, "height": 1080,
                                   "avg_frame_rate": "30/1"})
        self.assertIsNone(props["rate"])

    def test_vc1_to_hevc_matches_actual_progressive_output_in_both_tables(self):
        props = stream_descriptor({"id": "0x1011", "codec_name": "hevc", "width": 1920, "height": 1080,
                                   "avg_frame_rate": "30000/1001", "field_order": "progressive",
                                   "color_primaries": "bt709", "color_transfer": "bt709"})
        for raw, entries, reconcile in (
            (clpi(0xEA, 0x44), clpi_video_entries, lambda d: reconcile_clpi(d, {0x1011: props})),
            (mpls(0xEA, 0x44), mpls_video_entries, lambda d: reconcile_mpls(d, {"00001": {0x1011: props}})),
        ):
            data = bytearray(raw)
            offset = list(entries(data))[0][-2]
            self.assertEqual(reconcile(data), 1)
            self.assertEqual(data[offset:offset + 2], b"\x24\x64")
            self.assertEqual(len(data), len(raw))
            self.assertEqual(reconcile(data), 0)

    def test_unknown_colour_is_not_invented(self):
        props = stream_descriptor({"id": "0x1011", "codec_name": "hevc", "width": 1280, "height": 720,
                                   "avg_frame_rate": "60000/1001", "pix_fmt": "yuv420p"})
        self.assertIsNone(props["color_space"])
        self.assertFalse(props["inferred_bd709"])

    def test_out_of_mux_and_other_clip_are_not_patched(self):
        props = {0x1011: {"codec": 0x24, "format": 6}}
        for raw in (mpls(kind=2), mpls(clip="00002")):
            data = bytearray(raw)
            self.assertEqual(reconcile_mpls(data, {"00001": props}), 0)
            self.assertEqual(data, raw)

    def test_malformed_tables_fail_without_changing_data(self):
        for raw, call in ((clpi()[:-1], lambda d: reconcile_clpi(d, {0x1011: {"codec": 0x24}})),
                          (mpls()[:-1], lambda d: reconcile_mpls(d, {"00001": {0x1011: {"codec": 0x24}}}))):
            data = bytearray(raw)
            with self.assertRaises(ToolError):
                call(data)
            self.assertEqual(data, raw)


class RepairJournalTests(unittest.TestCase):
    def test_invalid_undo_journal_cannot_resize_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.iso"
            path.write_bytes(b"abcd")
            journal = Path(directory) / "undo.json"
            journal.write_text(json.dumps({"schema": "bd2hevc-byte-repair-v1", "path": str(path), "size": 4,
                                           "patches": [{"offset": 2, "before": "6364", "after": "434445"}]}))
            with self.assertRaises(UdfValidationError):
                restore_journal(journal)
            self.assertEqual(path.read_bytes(), b"abcd")

    def test_apply_undo_and_stale_source_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.iso"
            path.write_bytes(b"abcdefgh")
            st = path.stat()
            patches = [{"offset": 2, "before": b"cd".hex(), "after": b"CD".hex()}]
            journal = apply_patches(path, patches, Path(directory) / "undo", expected_stat=(st.st_size, st.st_mtime_ns))
            self.assertEqual(path.read_bytes(), b"abCDefgh")
            with self.assertRaises(UdfValidationError):
                apply_patches(path, patches, Path(directory) / "undo", expected_stat=(st.st_size, st.st_mtime_ns))
            restore_journal(journal)
            self.assertEqual(path.read_bytes(), b"abcdefgh")
            path.write_bytes(b"abXXefgh")
            with self.assertRaises(UdfValidationError):
                restore_journal(journal)
            self.assertEqual(path.read_bytes(), b"abXXefgh")

    def test_out_of_bounds_patch_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.iso"
            path.write_bytes(b"abcd")
            st = path.stat()
            with self.assertRaises(UdfValidationError):
                apply_patches(path, [{"offset": 4, "before": "61", "after": "62"}], Path(directory),
                              expected_stat=(st.st_size, st.st_mtime_ns))
            self.assertEqual(path.read_bytes(), b"abcd")


class UdfWriterIntegrationTests(unittest.TestCase):
    def test_real_encode_retains_colour_in_sps(self):
        root = Path(__file__).resolve().parents[1]
        ffmpeg = root / "tools/ffmpeg/bin/ffmpeg.exe"
        ffprobe = root / "tools/ffmpeg/bin/ffprobe.exe"
        if not ffmpeg.exists() or not ffprobe.exists():
            self.skipTest("Bundled FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.m2ts"
            output = Path(directory) / "output.m2ts"
            subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=24",
                            "-t", "0.25", "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
                            "-c:v", "libx264", "-mpegts_m2ts_mode", "1", str(source)], check=True, capture_output=True)
            video = {"fps": 24, "target_hevc": {"target_bps": 1000000}, "color_primaries": "bt709",
                     "color_transfer": "bt709", "color_space": "bt709", "color_range": "tv"}
            command = encoding.encode_to_hevc_m2ts(source, output, {"video": video}, {"ffmpeg": str(ffmpeg)},
                                                    encoder="libx265", dry_run=True)
            subprocess.run(command, check=True, capture_output=True)
            probe = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v", "-show_streams", "-of", "json", str(output)],
                                    check=True, capture_output=True, text=True)
            stream = json.loads(probe.stdout)["streams"][0]
            for field in ("color_primaries", "color_transfer", "color_space", "color_range"):
                self.assertEqual(stream[field], video[field])

    def test_new_writer_and_legacy_repair_preserve_payload(self):
        tool = Path(__file__).resolve().parents[1] / "tools/hadris-udf/bin/hadris-udf.exe"
        if os.name != 'nt' or not tool.exists():
            self.skipTest("Bundled Windows ISO writer is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "BDMV/CLIPINF").mkdir(parents=True)
            (source / "BDMV/CLIPINF/00001.clpi").write_bytes(clpi())
            (source / "payload.bin").write_bytes(bytes(range(256)) * 100)
            path = root / "test.iso"
            subprocess.run([str(tool), "create", str(source), "--output", str(path), "--revision", "2.50"],
                           check=True, capture_output=True)
            self.assertTrue(validate_udf(path)["ok"])
            original = path.read_bytes()
            image = UdfImage(path)
            self.assertEqual(image.read("payload.bin"), (source / "payload.bin").read_bytes())
            # Reproduce both historical writer bugs with otherwise valid CRCs.
            damaged = bytearray(original)
            for descriptor in image.volume["descriptors"]:
                offset = descriptor["sector"] * BLOCK
                data = damaged[offset:offset + BLOCK]
                if descriptor["tag"] == 6:
                    data[20:84] = bytes(64)
                    update_tag(data)
                elif descriptor["tag"] == 8:
                    update_tag(data, 0)
                damaged[offset:offset + BLOCK] = data
            path.write_bytes(damaged)
            inspection = inspect_udf(path)
            self.assertEqual(len(inspection["issues"]), 4)
            self.assertEqual(len(inspection["patches"]), 4)
            st = path.stat()
            journal = apply_patches(path, inspection["patches"], root / "undo", expected_stat=(st.st_size, st.st_mtime_ns))
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(validate_udf(path)["ok"])
            restore_journal(journal)
            self.assertEqual(path.read_bytes(), damaged)


if __name__ == "__main__":
    unittest.main()
