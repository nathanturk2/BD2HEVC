import argparse
import contextlib
import io
import subprocess
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bd2hevc_app import core as bd
from bd2hevc_app import bdj, bitrate, config, encoding, muxing, navigation, output, progress, queueing, repair, scan, tools, validation


class ModuleSplitTests(unittest.TestCase):
    def test_cli_help_points_to_command_specific_examples(self) -> None:
        parser = bd.build_parser()
        help_text = parser.format_help()

        self.assertIn("Command help:", help_text)
        self.assertIn("py bd2hevc.py <command> --help", help_text)
        self.assertIn("py bd2hevc.py queue --help", help_text)

        with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["queue", "--help"])
            queue_help = buffer.getvalue()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Examples:", queue_help)
        self.assertIn('Examples:\n  py bd2hevc.py queue "BD backups', queue_help)
        self.assertIn('py bd2hevc.py queue "BD backups', queue_help)

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

    def test_ffmpeg_carriage_return_stats_update_current_encode_time(self) -> None:
        with TemporaryDirectory() as temp:
            log = Path(temp) / "job.log"
            log.write_text(
                "BD2HEVC_PROGRESS encode-start 00001.m2ts\r"
                "frame= 100 time=00:00:04.00 speed=8.0x\r"
                "frame= 200 time=00:00:08.00 speed=9.0x\r",
                encoding="utf-8",
            )

            state = progress.latest_log_progress(log)

        self.assertEqual(state["encode_file"], "00001.m2ts")
        self.assertEqual(state["encode_seconds"], 8.0)
        self.assertEqual(state["encode_speed"], "9.0x")

    def test_top_progress_tracks_encoding_not_active_mux(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            plan = root / "plan.json"
            log = root / "job.log"
            plan.write_text(
                json.dumps(
                    {
                        "reencode_clips": [
                            {"file": "00001.m2ts", "duration": 100.0},
                            {"file": "00002.m2ts", "duration": 100.0},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            log.write_text(
                "\n".join(
                    [
                        "BD2HEVC_PROGRESS encode-start 00001.m2ts",
                        "frame= 100 time=00:01:40.00 speed=4.0x",
                        "BD2HEVC_PROGRESS encode-done 00001.m2ts",
                        "BD2HEVC_PROGRESS mux-start 00001.m2ts",
                        "10.0% complete",
                        "BD2HEVC_PROGRESS encode-start 00002.m2ts",
                        "frame= 100 time=00:00:20.00 speed=3.0x",
                    ]
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(target=str(root / "out"), plan=str(plan), log=str(log), width=32, watch=0)

            lines = progress.progress_lines(args, inspect_outputs=False)

        self.assertIn("60.0% encoded", lines[0])
        self.assertIn("encoded clips: 1/2 complete", lines[1])
        self.assertTrue(any("muxing:" in line and "10.0%" in line for line in lines))

    def test_progress_reports_audio_as_parallel_side_work(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            plan = root / "plan.json"
            log = root / "job.log"
            plan.write_text(
                json.dumps({"reencode_clips": [{"file": "00001.m2ts", "duration": 120.0}, {"file": "00002.m2ts", "duration": 120.0}]}),
                encoding="utf-8",
            )
            log.write_text(
                "\n".join(
                    [
                        "BD2HEVC_PROGRESS encode-start 00001.m2ts",
                        "frame= 100 time=00:02:00.00 speed=4.0x",
                        "BD2HEVC_PROGRESS encode-done 00001.m2ts",
                        "BD2HEVC_PROGRESS audio-start 00001.m2ts",
                        "size= 1000KiB time=00:00:30.00 bitrate=256.0kbits/s speed=40.0x",
                        "BD2HEVC_PROGRESS encode-start 00002.m2ts",
                        "frame= 100 time=00:00:24.00 speed=3.0x",
                    ]
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(target=str(root / "out"), plan=str(plan), log=str(log), width=32, watch=0)

            lines = progress.progress_lines(args, inspect_outputs=False)

        self.assertIn("60.0% encoded", lines[0])
        self.assertIn("stage: audio + encoding", lines[0])
        self.assertTrue(any(line.startswith("audio:") and "25.0%" in line for line in lines))
        self.assertTrue(any(line.startswith("encoding:") and "20.0%" in line for line in lines))


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

    def test_compact_cq_uses_cq_over_default_reencode_cutoff(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=11,
            source_codec="h264",
            mode="compact-cq",
        )
        self.assertEqual(plan["rate_control"], "cq")
        self.assertEqual(plan["cq"], 18)
        self.assertNotIn("fallback_vbr", plan)

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

    def test_compact_cq_uses_smaller_below_default_reencode_cutoff(self) -> None:
        plan = bd.equivalent_hevc_bitrate(
            video_bps=12_000_000,
            width=1920,
            height=1080,
            fps=23.976,
            duration_seconds=9,
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
    def test_disc_title_normalizer_preserves_acronyms_and_roman_numerals(self) -> None:
        self.assertEqual(output.disc_title_from_folder_name("BACK_TO_THE_FUTURE_PART_II"), "Back to the Future Part II")
        self.assertEqual(output.disc_title_from_folder_name("BBC_PRIDE_AND_PREJUDICE"), "BBC Pride and Prejudice")
        self.assertEqual(output.disc_title_from_folder_name("my_dvd_backup"), "My DVD Backup")
        self.assertEqual(output.disc_title_from_folder_name("IT"), "IT")
        self.assertEqual(output.disc_title_from_folder_name("It"), "It")
        self.assertEqual(output.disc_title_from_folder_name("PRIDE_PREJUDICE_2005_film"), "Pride Prejudice 2005 Film")

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

    def test_compact_stereo_audio_reencodes_audio_without_subtitles_in_temp_media(self) -> None:
        clip_info = {
            "video": {
                "fps": 23.976,
                "target_hevc": {
                    "mode": "compact-cq",
                    "rate_control": "cq",
                    "cq": 20,
                    "maxrate_bps": 80_000_000,
                    "bufsize_bps": 160_000_000,
                },
            },
            "audio": [
                {"codec_name": "truehd", "channels": 8, "language": "eng"},
                {"codec_name": "ac3", "channels": 1, "language": "jpn"},
            ],
        }
        cmd = bd.encode_to_hevc_m2ts(
            Path("in.m2ts"),
            Path("out.m2ts"),
            clip_info,
            {"ffmpeg": "ffmpeg"},
            audio_mode="compact-stereo",
            dry_run=True,
        )
        self.assertIn("-c:a", cmd)
        self.assertIn("ac3", cmd)
        self.assertIn("-ac:a:0", cmd)
        self.assertIn("2", cmd)
        self.assertIn("-ac:a:1", cmd)
        self.assertIn("1", cmd)
        self.assertIn("-b:a:0", cmd)
        self.assertIn("256k", cmd)
        self.assertIn("-b:a:1", cmd)
        self.assertIn("128k", cmd)
        self.assertNotIn("0:s?", cmd)
        self.assertIn("-f", cmd)
        self.assertIn("mpegts", cmd)

    def test_compact_audio_tracks_are_elementary_ac3_files(self) -> None:
        clip_info = {
            "audio": [
                {"index": 1, "codec_name": "truehd", "channels": 8, "language": "eng"},
                {"index": 2, "codec_name": "ac3", "channels": 1, "language": "jpn"},
            ],
        }
        outputs, cmd = bd.transcode_compact_audio_tracks(
            Path("in.m2ts"),
            Path("work/00001.compact-audio"),
            clip_info,
            {"ffmpeg": "ffmpeg"},
            dry_run=True,
        )
        self.assertEqual([Path(item["path"]).suffix for item in outputs], [".ac3", ".ac3"])
        self.assertEqual(outputs[0]["channels"], 2)
        self.assertEqual(outputs[1]["channels"], 1)
        self.assertIn("-map", cmd)
        self.assertIn("0:1", cmd)
        self.assertIn("0:2", cmd)
        self.assertNotIn("0:s?", cmd)

    def test_compact_audio_skips_unplayable_zero_channel_streams(self) -> None:
        clip_info = {
            "audio": [
                {"index": 1, "codec_name": "pcm_bluray", "channels": 2, "language": "eng"},
                {"index": 2, "codec_name": "pcm_bluray", "channels": 2, "language": "eng"},
                {"index": 3, "codec_name": "mp3", "channels": 0},
            ],
        }
        outputs, cmd = bd.transcode_compact_audio_tracks(
            Path("in.m2ts"),
            Path("work/00123.compact-audio"),
            clip_info,
            {"ffmpeg": "ffmpeg"},
            dry_run=True,
        )

        self.assertEqual(len(outputs), 2)
        self.assertIn("0:1", cmd)
        self.assertIn("0:2", cmd)
        self.assertNotIn("0:3", cmd)
        self.assertEqual([Path(item["path"]).name for item in outputs], ["00123.audio00.ac3", "00123.audio01.ac3"])

    def test_main_title_cq_override_targets_longest_reencoded_cq_clip(self) -> None:
        clips = [
            {"file": "00001.m2ts", "action": "reencode", "duration": 30.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
            {"file": "00002.m2ts", "action": "reencode", "duration": 7200.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
            {"file": "00003.m2ts", "action": "reencode", "duration": 8000.0, "video": {"target_hevc": {"rate_control": "vbr", "target_bps": 5_000_000}}},
        ]
        report = bd.apply_main_title_cq_override(clips, 18)
        self.assertEqual(report["file"], "00002.m2ts")
        self.assertEqual(clips[0]["video"]["target_hevc"]["cq"], 20)
        self.assertEqual(clips[1]["video"]["target_hevc"]["cq"], 18)
        self.assertNotIn("cq", clips[2]["video"]["target_hevc"])

    def test_top_n_cq_override_targets_longest_reencoded_cq_clips(self) -> None:
        clips = [
            {"file": "00001.m2ts", "action": "reencode", "duration": 30.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
            {"file": "00002.m2ts", "action": "reencode", "duration": 7200.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
            {"file": "00003.m2ts", "action": "reencode", "duration": 3600.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
            {"file": "00004.m2ts", "action": "reencode", "duration": 8000.0, "video": {"target_hevc": {"rate_control": "vbr", "target_bps": 5_000_000}}},
            {"file": "00005.m2ts", "action": "copy", "duration": 9000.0, "video": {"target_hevc": {"rate_control": "cq", "cq": 20}}},
        ]

        report = bd.apply_top_n_cq_override(clips, [2, 18])

        self.assertEqual(report["matched_count"], 2)
        self.assertEqual([item["file"] for item in report["clips"]], ["00002.m2ts", "00003.m2ts"])
        self.assertEqual(clips[0]["video"]["target_hevc"]["cq"], 20)
        self.assertEqual(clips[1]["video"]["target_hevc"]["cq"], 18)
        self.assertEqual(clips[2]["video"]["target_hevc"]["cq"], 18)
        self.assertNotIn("cq", clips[3]["video"]["target_hevc"])

    def test_top_n_cq_rejects_main_title_cq_combination(self) -> None:
        args = argparse.Namespace(main_title_cq=18, top_n_cq=[3, 18])

        with self.assertRaisesRegex(bd.ToolError, "cannot be used"):
            bd.validate_cq_override_args(args)

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
            main_title_cq=18,
            top_n_cq=None,
            audio_mode="compact-stereo",
            stereo_audio_bitrate=256_000,
            mono_audio_bitrate=128_000,
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
        self.assertIn("--main-title-cq", cmd)
        self.assertIn("18", cmd)
        self.assertIn("--audio-mode", cmd)
        self.assertIn("compact-stereo", cmd)

    def test_background_command_carries_top_n_cq(self) -> None:
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
            anime_cq_min_duration=config.DEFAULT_ANIME_CQ_MIN_DURATION,
            main_title_cq=None,
            top_n_cq=[3, 18],
            audio_mode="compact-stereo",
            stereo_audio_bitrate=256_000,
            mono_audio_bitrate=128_000,
            vlc_compat=bd.DEFAULT_VLC_COMPATIBILITY_MODE,
            vlc_fix=[],
            compat_patch_file=[],
            decode_sample=30.0,
        )
        cmd = bd.auto_command_for_job(args, Path("out"), Path("report.json"))
        self.assertIn("--top-n-cq", cmd)
        index = cmd.index("--top-n-cq")
        self.assertEqual(cmd[index + 1 : index + 3], ["3", "18"])

    def test_compact_audio_pipeline_runs_audio_before_mux_per_clip(self) -> None:
        args = argparse.Namespace(encode_ahead_depth=2, audio_mode="compact-stereo")
        contexts = [
            {"file": "00001.m2ts", "clip": {"duration": 1.0}},
            {"file": "00002.m2ts", "clip": {"duration": 2.0}},
        ]
        events: list[tuple[str, str]] = []

        def fake_encode(ctx: dict, tools: dict, args: argparse.Namespace) -> dict:
            events.append(("encode", ctx["file"]))
            return ctx

        def fake_audio(ctx: dict, tools: dict, args: argparse.Namespace) -> dict:
            events.append(("audio", ctx["file"]))
            return ctx

        def fake_mux(ctx: dict, tools: dict, args: argparse.Namespace) -> dict:
            events.append(("mux", ctx["file"]))
            return {"file": ctx["file"]}

        with (
            mock.patch.object(bd, "encode_clone_clip_context", side_effect=fake_encode),
            mock.patch.object(bd, "transcode_compact_audio_context", side_effect=fake_audio),
            mock.patch.object(bd, "mux_validate_clone_clip_context", side_effect=fake_mux),
            mock.patch.object(bd, "emit_conversion_progress"),
            mock.patch.object(bd, "progress_event") as progress_event,
        ):
            validations, done_seconds = bd.run_queued_encode_audio_mux_pipeline(
                contexts,
                {},
                args,
                total_seconds=3.0,
                progress_enabled=False,
            )

        self.assertEqual([item["file"] for item in validations], ["00001.m2ts", "00002.m2ts"])
        self.assertEqual(done_seconds, 3.0)
        for clip_file in ("00001.m2ts", "00002.m2ts"):
            clip_events = [index for index, event in enumerate(events) if event[1] == clip_file]
            self.assertEqual([events[index][0] for index in clip_events], ["encode", "audio", "mux"])
        progress_event.assert_any_call("pipeline", "enabled", mode="encode-audio-mux-queue", depth=2)

    def test_job_loader_skips_transient_empty_job_files(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "empty.job.json"
            path.write_text("", encoding="utf-8")

            self.assertIsNone(queueing.try_load_job(path, attempts=1))

    def test_save_job_writes_readable_json(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "job.job.json"
            queueing.save_job(path, {"id": "job-1", "status": "queued"})

            self.assertEqual(queueing.load_job(path)["id"], "job-1")

    def test_save_job_retries_transient_windows_replace_denial(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "job.job.json"
            attempts = {"count": 0}
            real_replace = queueing.os.replace

            def flaky_replace(source: Path, target: Path) -> None:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise PermissionError("locked")
                real_replace(source, target)

            with mock.patch.object(queueing.os, "replace", side_effect=flaky_replace), mock.patch.object(queueing.time, "sleep"):
                queueing.save_job(path, {"id": "job-1", "status": "queued"})

            self.assertGreaterEqual(attempts["count"], 2)
            self.assertEqual(queueing.load_job(path)["id"], "job-1")

    def test_status_handles_job_before_progress_plan_exists(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = {
                "id": "job-1",
                "status": "queued",
                "source": str(root / "SOURCE_DISC"),
                "output": str(root / "Output Disc (BD) (UHD converted)"),
                "plan": str(root / "missing.plan.json"),
                "log": str(root / "job.log"),
                "exitcode": str(root / "job.exitcode.txt"),
            }

            lines = queueing.job_status_lines(job, width=32)

        self.assertIn("planning scan has not finished yet", "\n".join(lines))
        self.assertIn("encoded clips: unknown", "\n".join(lines))

    def test_queued_status_shows_all_earlier_active_jobs(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(queueing, "DEFAULT_JOB_DIR", root), mock.patch.object(queueing, "pid_is_running", return_value=True):
                for index, status in ((1, "running"), (2, "queued"), (3, "queued")):
                    path = root / f"job-{index}.job.json"
                    queueing.save_job(
                        path,
                        {
                            "id": f"job-{index}",
                            "status": status,
                            "pid": 1000 + index,
                            "queue_order": float(index),
                            "job_file": str(path),
                            "source": str(root / f"source-{index}"),
                            "output": str(root / f"Output {index} (BD) (UHD converted)"),
                            "plan": str(root / f"job-{index}.plan.json"),
                            "log": str(root / f"job-{index}.log"),
                            "exitcode": str(root / f"job-{index}.exitcode.txt"),
                        },
                    )

                job = queueing.load_job(root / "job-3.job.json")
                lines = "\n".join(queueing.job_status_lines(job, width=80))

        self.assertIn("Queue: 2 jobs ahead", lines)
        self.assertIn("Next ahead: job-1", lines)
        self.assertIn("Also ahead: job-2", lines)

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

    def test_silent_menu_clip_gets_sparse_timing_detection(self) -> None:
        probe = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mpeg2video",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24000/1001",
                    "r_frame_rate": "24000/1001",
                    "start_time": "11.650667",
                    "duration": "65.106700",
                }
            ],
            "format": {"duration": "65.106700", "bit_rate": "896120"},
        }
        with (
            mock.patch.object(scan, "ffprobe_streams", return_value=probe),
            mock.patch.object(scan, "count_video_frames", return_value=14),
        ):
            info = scan.inspect_clip(Path("00491.m2ts"), {"ffprobe": "ffprobe"})

        self.assertTrue(info["video"]["sparse_timestamp_video"])
        self.assertEqual(info["video"]["decoded_frame_count"], 14)


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

