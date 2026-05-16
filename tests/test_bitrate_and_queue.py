import argparse
import subprocess
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bd2hevc_app import core as bd
from bd2hevc_app import bdj, bitrate, config, encoding, muxing, navigation, output, progress, queueing, repair, scan, tools, validation


class ModuleSplitTests(unittest.TestCase):
    def test_public_modules_import_core_helpers(self) -> None:
        self.assertEqual(config.VERSION, bd.VERSION)
        self.assertIs(bitrate.equivalent_hevc_bitrate, bd.equivalent_hevc_bitrate)
        self.assertIs(tools.discover_tools, bd.discover_tools)
        self.assertIs(scan.inspect_clip, bd.inspect_clip)
        self.assertIs(validation.validate_clip, bd.validate_clip)
        self.assertIs(output.copy_disc_tree_skipping_reencoded_streams, bd.copy_disc_tree_skipping_reencoded_streams)
        self.assertIs(progress.progress_event, bd.progress_event)
        self.assertIs(queueing.job_paths, bd.job_paths)
        self.assertIs(queueing.cmd_status, bd.cmd_status)
        self.assertIs(bdj.patch_known_bdj_compatibility, bd.patch_known_bdj_compatibility)
        self.assertIs(bdj.patch_bluray_vlc_menu, bd.patch_bluray_vlc_menu)
        self.assertIs(encoding.encode_to_hevc_m2ts, bd.encode_to_hevc_m2ts)
        self.assertIs(muxing.author_m2ts_split, bd.author_m2ts_split)
        self.assertIs(muxing.write_tsmuxer_meta, bd.write_tsmuxer_meta)
        self.assertIs(navigation.patch_navigation_for_hevc, bd.patch_navigation_for_hevc)
        self.assertIs(navigation.restore_source_clpi, bd.restore_source_clpi)
        self.assertIs(repair.select_output_repair_clips, bd.select_output_repair_clips)
        self.assertIs(repair.reencode_replacement_clip, bd.reencode_replacement_clip)
        self.assertIs(progress.fit_terminal_line, bd.fit_terminal_line)

    def test_watch_lines_are_truncated_to_terminal_width(self) -> None:
        line = "Log: " + ("x" * 120)
        fitted = progress.fit_terminal_line(line, 40)
        self.assertLessEqual(len(fitted), 40)
        self.assertTrue(fitted.endswith("..."))

    def test_progress_markers_after_carriage_returns_are_counted(self) -> None:
        with TemporaryDirectory() as temp:
            log = Path(temp) / "job.log"
            log.write_text(
                "frame= 100 time=00:00:04.00    \rBD2HEVC_PROGRESS validate-done 00000.m2ts\r\n"
                "frame= 200 time=00:00:08.00    \rBD2HEVC_PROGRESS validate-done 00001.m2ts\r\n",
                encoding="utf-8",
            )
            state = progress.latest_log_progress(log)
            self.assertEqual(state["done_files"], ["00000.m2ts", "00001.m2ts"])


class BitratePresetTests(unittest.TestCase):
    def test_balanced_avc_curve_tracks_hevc_source_equivalent_target(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=30_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=120 * 60,
            source_codec="h264",
            mode="balanced",
        )
        self.assertEqual(plan["rate_control"], "vbr")
        self.assertEqual(plan["factor"], 0.55)

    def test_transparent_keeps_extra_margin_over_balanced(self) -> None:
        balanced = bd.equivalent_hevc_bitrate(
            video_bps=30_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=120 * 60,
            source_codec="h264",
            mode="balanced",
        )
        transparent = bd.equivalent_hevc_bitrate(
            video_bps=30_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=120 * 60,
            source_codec="h264",
            mode="transparent",
        )
        self.assertGreater(transparent["target_bps"], balanced["target_bps"])

    def test_compact_cq_uses_cq_over_default_cutoff(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=11 * 60,
            source_codec="h264",
            mode="compact-cq",
        )
        self.assertEqual(plan["rate_control"], "cq")
        self.assertEqual(plan["cq"], 18)

    def test_compact_cq_value_is_configurable(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=11 * 60,
            source_codec="h264",
            mode="compact-cq",
            compact_cq_value=20,
        )
        self.assertEqual(plan["rate_control"], "cq")
        self.assertEqual(plan["cq"], 20)

    def test_compact_cq_uses_smaller_for_short_clips(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=9 * 60,
            source_codec="h264",
            mode="compact-cq",
        )
        self.assertEqual(plan["rate_control"], "vbr")
        self.assertEqual(plan["fallback_bitrate_mode"], "smaller")

    def test_bitrate_preset_file_sets_mode_bounds_and_cq(self) -> None:
        with TemporaryDirectory() as temp:
            preset = Path(temp) / "preset.json"
            preset.write_text(
                json.dumps(
                    {
                        "mode": "compact-cq",
                        "compact_cq_value": 20,
                        "compact_cq_min_duration": "7m",
                        "max_video_bitrate": "70M",
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                bitrate_preset_file=str(preset),
                bitrate_mode="balanced",
                hevc_bitrate_factor=None,
                min_video_bitrate=2_000_000,
                max_video_bitrate=80_000_000,
                maxrate_multiplier=1.55,
                bufsize_multiplier=2.0,
                anime_cq_min_duration=config.DEFAULT_ANIME_CQ_MIN_DURATION,
                compact_cq_value=config.ANIME_CQ_VALUE,
            )
            options = bd.bitrate_options_from_args(args)

        self.assertEqual(options["mode"], "compact-cq")
        self.assertEqual(options["compact_cq_value"], 20)
        self.assertEqual(options["anime_cq_min_duration"], 7 * 60)
        self.assertEqual(options["max_bps"], 70_000_000)


class CopyPlanningTests(unittest.TestCase):
    def test_preservation_copy_skips_only_reencoded_streams(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "src"
            output = root / "out"
            stream = source / "BDMV" / "STREAM"
            playlist = source / "BDMV" / "PLAYLIST"
            stream.mkdir(parents=True)
            playlist.mkdir(parents=True)
            (stream / "00000.m2ts").write_bytes(b"replace")
            (stream / "00001.m2ts").write_bytes(b"copy")
            (playlist / "00000.mpls").write_bytes(b"playlist")

            report = bd.copy_disc_tree_skipping_reencoded_streams(source, output, {"00000.m2ts"})

            self.assertFalse((output / "BDMV" / "STREAM" / "00000.m2ts").exists())
            self.assertEqual((output / "BDMV" / "STREAM" / "00001.m2ts").read_bytes(), b"copy")
            self.assertEqual((output / "BDMV" / "PLAYLIST" / "00000.mpls").read_bytes(), b"playlist")
            self.assertEqual(report["skipped_reencode_stream_files"], 1)

    def test_missing_output_drive_is_reported_before_background_start(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "src"
            source.mkdir()
            missing = Path("Z:/bd2hevc_missing_drive_test")
            if missing.anchor and not Path(missing.anchor).exists():
                with self.assertRaisesRegex(bd.ToolError, "Output drive or root does not exist"):
                    bd.make_output_available(missing, source, force=False)


class CommandConstructionTests(unittest.TestCase):
    def test_compact_cq_nvenc_uses_lean_handbrake_like_cq(self) -> None:
        clip_info = {
            "video": {
                "fps": 23.976,
                "target_hevc": {
                    "mode": "compact-cq",
                    "rate_control": "cq",
                    "cq": 18,
                    "maxrate_bps": 80_000_000,
                    "bufsize_bps": 160_000_000,
                },
            }
        }
        cmd = bd.encode_to_hevc_m2ts(
            Path("in.m2ts"),
            Path("out.m2ts"),
            clip_info,
            {"ffmpeg": "ffmpeg"},
            dry_run=True,
        )
        self.assertIn("-cq", cmd)
        self.assertIn("18", cmd)
        self.assertNotIn("-spatial-aq", cmd)
        self.assertNotIn("-temporal-aq", cmd)
        self.assertNotIn("-maxrate:v:0", cmd)
        self.assertNotIn("-bufsize:v:0", cmd)
        self.assertNotIn("-bluray-compat", cmd)

    def test_background_command_carries_compact_cq_cutoff(self) -> None:
        args = argparse.Namespace(
            source="Disc",
            fast_bitrate=False,
            force_encode=False,
            force=False,
            makemkv=False,
            no_makemkv=False,
            require_makemkv=False,
            no_patch_navigation=False,
            no_bdj_compatibility_patches=False,
            no_encode_ahead=False,
            verbose=False,
            hevc_bit_depth=8,
            encoder="hevc_nvenc",
            encode_ahead_depth=3,
            bitrate_preset_file=None,
            bitrate_mode="compact-cq",
            hevc_bitrate_factor=None,
            min_video_bitrate=2_000_000,
            max_video_bitrate=80_000_000,
            maxrate_multiplier=1.55,
            bufsize_multiplier=2.0,
            compact_cq_value=20,
            anime_cq_min_duration=12 * 60.0,
            vlc_compat=bd.DEFAULT_VLC_COMPATIBILITY_MODE,
            vlc_fix=[],
            compat_patch_file=[],
            decode_sample=30.0,
        )
        cmd = bd.auto_command_for_job(args, Path("out"), Path("report.json"))
        self.assertIn("--bitrate-mode", cmd)
        self.assertIn("compact-cq", cmd)
        self.assertIn("--compact-cq-value", cmd)
        self.assertIn("20", cmd)
        self.assertIn("--compact-cq-min-duration", cmd)
        self.assertIn("720.0", cmd)

    def test_legacy_anime_cq18_alias_still_maps_to_compact_cq(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=11 * 60,
            source_codec="h264",
            mode="anime-cq18",
        )
        self.assertEqual(plan["mode"], "compact-cq")
        self.assertEqual(plan["rate_control"], "cq")

    def test_legacy_episode_compact_alias_still_maps_to_compact_cq(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=11 * 60,
            source_codec="h264",
            mode="episode-compact",
        )
        self.assertEqual(plan["mode"], "compact-cq")
        self.assertEqual(plan["rate_control"], "cq")


class LinuxCompatibilityTests(unittest.TestCase):
    def test_posix_tool_discovery_ignores_windows_executables(self) -> None:
        def fake_which(name: str, *, path: str | None = None) -> str | None:
            native = {
                "ffmpeg": "/usr/bin/ffmpeg",
                "ffprobe": "/usr/bin/ffprobe",
                "makemkvcon": "/usr/bin/makemkvcon",
                "tsmuxer": "/usr/local/bin/tsmuxer",
                "vlc": "/usr/bin/vlc",
            }
            if name in native:
                return native[name]
            if name.lower().endswith(".exe"):
                return f"/mnt/c/WindowsApps/{name}"
            return None

        encoders = " V..... hevc_nvenc NVIDIA NVENC hevc encoder\n V..... libx265 libx265 H.265 / HEVC\n"
        completed = subprocess.CompletedProcess(["ffmpeg"], 0, stdout=encoders, stderr="")
        with (
            mock.patch.object(tools.os, "name", "posix"),
            mock.patch.object(tools, "LOCAL_TSMUXERS", []),
            mock.patch.object(tools, "MAKEMKV_DIRS", []),
            mock.patch.object(tools, "VLC_DIRS", []),
            mock.patch.object(tools, "shutil_which", side_effect=fake_which),
            mock.patch.object(tools, "run_cmd", return_value=completed),
        ):
            found = tools.discover_tools()

        self.assertEqual(found["ffmpeg"], "/usr/bin/ffmpeg")
        self.assertEqual(found["ffprobe"], "/usr/bin/ffprobe")
        self.assertEqual(found["makemkvcon"], "/usr/bin/makemkvcon")
        self.assertEqual(found["tsmuxer"], "/usr/local/bin/tsmuxer")
        self.assertEqual(found["vlc"], "/usr/bin/vlc")
        self.assertFalse(str(found["ffmpeg"]).lower().endswith(".exe"))
        self.assertIn("hevc_nvenc", found["hevc_encoders"])

    def test_posix_background_process_uses_new_session_not_creationflags(self) -> None:
        with mock.patch.object(queueing.os, "name", "posix"):
            self.assertEqual(queueing.process_creationflags(detached=True, new_group=True), 0)
            self.assertEqual(queueing.process_kwargs(detached=True, new_group=True), {"start_new_session": True})
            self.assertEqual(queueing.process_kwargs(), {})


if __name__ == "__main__":
    unittest.main()

