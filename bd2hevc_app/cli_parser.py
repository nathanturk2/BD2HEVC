"""Cli parser: explicit services preserve the public facade and test seams."""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Any, Callable

def build_parser(services) -> argparse.ArgumentParser:
    parser = services.argparse.ArgumentParser(
        description="BD2HEVC: convert local Blu-ray backups to HEVC while preserving menus, extras, audio, and subtitles.",
        epilog=(
            "Common commands:\n"
            "  py bd2hevc.py queue \"BD backups\" --output-dir \"Converted UHD-BD\"\n"
            "  py bd2hevc.py status --watch\n"
            "  py bd2hevc.py clips \"BD backups\\Movie Disc\"\n"
            "  py bd2hevc.py preset list\n"
            "  py bd2hevc.py jobs\n"
            "  py bd2hevc.py diagnose \"Converted UHD-BD\\Movie (BD) (UHD converted)\"\n"
            "  py bd2hevc.py record-libbluray \"Converted UHD-BD\\Movie (BD) (UHD converted)\"\n"
            "\n"
            "Command help:\n"
            "  py bd2hevc.py <command> --help\n"
            "  py bd2hevc.py queue --help"
        ),
        formatter_class=services.argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"BD2HEVC {services.VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gui = services.command_parser(sub, "gui", help="Open the BD2HEVC Windows graphical interface.", description="Open the full conversion, playlist, batch, queue, progress, preset, and maintenance interface.", examples="""
  py bd2hevc.py gui
""")
    p_gui.set_defaults(func=services.cmd_gui)

    p_tools = services.command_parser(sub, "tools", help="Show discovered external tools and HEVC encoder support.", description="Show the external programs BD2HEVC found and whether hardware HEVC encoding is available.", examples="""
  py bd2hevc.py tools
  py bd2hevc.py tools --json
""")
    p_tools.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_tools.set_defaults(func=services.cmd_tools)

    p_author_iso = services.command_parser(sub, "author-iso", help="Turn a Blu-ray folder backup into a UDF 2.50 ISO.", description="Create and verify a standards-appropriate UDF 2.50 image without modifying the source folder.", examples="""
  py bd2hevc.py author-iso "BD backups\\Movie Disc" "Movie Disc (BD).iso"
  py bd2hevc.py author-iso "BD backups\\Movie Disc" "Movie Disc.iso" --label MOVIE_DISC --report movie-iso.json
""")
    p_author_iso.add_argument("source", help="Complete Blu-ray folder backup containing BDMV.")
    p_author_iso.add_argument("output", help="Destination ISO path. .iso is appended when omitted.")
    p_author_iso.add_argument("--label", default=None, help="Optional UDF volume label. Defaults to the source folder name.")
    p_author_iso.add_argument("--iso-author-tool", default=None, help="Optional path to a compatible Hadris UDF author.")
    p_author_iso.add_argument("--force", action="store_true", help="Replace an existing destination ISO.")
    p_author_iso.add_argument("--report", default=None, help="Write a JSON authoring and verification report.")
    p_author_iso.add_argument("--json", action="store_true", help="Print the report as JSON.")
    p_author_iso.add_argument("--verbose", action="store_true")
    p_author_iso.set_defaults(func=services.cmd_author_iso)

    p_verify_iso = services.command_parser(sub, "verify-iso", help="Verify a UDF 2.50 Blu-ray ISO.", description="Run structural UDF checks and confirm that the image uses UDF revision 2.50.", examples="""
  py bd2hevc.py verify-iso "Movie Disc (BD).iso"
""")
    p_verify_iso.add_argument("image", help="ISO image to verify.")
    p_verify_iso.add_argument("--iso-author-tool", default=None, help="Optional path to a compatible Hadris UDF verifier.")
    p_verify_iso.add_argument("--reference", help="Compare all ISO payload files with this source folder.")
    p_verify_iso.add_argument("--manifest", help="Compare payload hashes with an authored .sha256.json sidecar.")
    p_verify_iso.add_argument("--json", action="store_true")
    p_verify_iso.add_argument("--verbose", action="store_true")
    p_verify_iso.set_defaults(func=services.cmd_verify_iso)

    p_preset = services.command_parser(sub, "preset", help="Save, list, show, and remove named presets.", description="Manage named conversion presets stored in the user config folder.", examples="""
  py bd2hevc.py preset save sarah --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
  py bd2hevc.py preset save source-mix --quality source-ratio:0.60 --codec-source-ratio h264=0.55 --codec-source-ratio mpeg2video=0.30
  py bd2hevc.py preset list
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD" --preset sarah
""")
    preset_sub = p_preset.add_subparsers(dest="preset_command", required=True)
    p_preset_list = preset_sub.add_parser("list", help="List saved and bundled presets.")
    p_preset_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_list.set_defaults(func=services.cmd_preset_list)

    p_preset_show = preset_sub.add_parser("show", help="Show one preset.")
    p_preset_show.add_argument("name", help="Preset name.")
    p_preset_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_show.set_defaults(func=services.cmd_preset_show)

    p_preset_save = preset_sub.add_parser("save", help="Save a named preset from command-line options.")
    p_preset_save.add_argument("name", help="Preset name. Use letters, numbers, dots, underscores, and hyphens.")
    p_preset_save.add_argument("--description", default=None, help="Optional short note shown by 'preset list'.")
    p_preset_save.add_argument("--force", action="store_true", help="Replace an existing preset.")
    p_preset_save.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_preset_save.add_argument("--encoder", choices=services.HEVC_ENCODERS, default="hevc_nvenc", help="Save a preferred HEVC encoder in the preset.")
    p_preset_save.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="Save a preferred HEVC output bit depth.")
    services.add_bitrate_args(p_preset_save, include_named_preset=False, include_file_preset=False)
    services.add_postprocess_args(p_preset_save)
    services.add_audio_args(p_preset_save)
    p_preset_save.set_defaults(func=services.cmd_preset_save_validated)

    p_preset_remove = preset_sub.add_parser("remove", aliases=["rm", "delete"], help="Remove a user preset.")
    p_preset_remove.add_argument("name", help="Preset name.")
    p_preset_remove.set_defaults(func=services.cmd_preset_remove)

    p_scan = services.command_parser(sub, "scan", help="Scan one or more BDMV backups with MakeMKV and FFprobe.", description="Inspect Blu-ray backup folders before conversion and write scan reports.", examples="""
  py bd2hevc.py scan "BD backups\\Movie Disc"
  py bd2hevc.py scan "BD backups" --no-makemkv
  py bd2hevc.py scan "BD backups\\Movie Disc" --accurate-video-bitrate
""")
    p_scan.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_scan.add_argument("--report-dir", default=str(services.DEFAULT_REPORT_DIR))
    p_scan.add_argument("--accurate-video-bitrate", action="store_true", help="Sum video packet sizes for bitrate. Slower, but best for encode planning.")
    services.add_bitrate_args(p_scan)
    services.add_postprocess_args(p_scan)
    services.add_makemkv_args(p_scan)
    p_scan.add_argument("--verbose", action="store_true")
    p_scan.set_defaults(func=services.cmd_scan)

    p_clips = services.command_parser(sub, "clips", help="List M2TS clip names, durations, and planned quality.", description="List the source clips in a Blu-ray backup so quality overrides can be chosen without reading raw JSON.", examples="""
  py bd2hevc.py clips "BD backups\\Movie Disc"
  py bd2hevc.py clips "BD backups\\Episode Disc" --quality cq:20 --top-n-quality 3 cq:18
  py bd2hevc.py clips "BD backups\\Menu-heavy Disc" --sort file --clip-quality 00012 copy
""")
    p_clips.add_argument("source", help="Source BD backup folder.")
    p_clips.add_argument("--sort", choices=["duration", "file"], default="duration", help="Sort by duration descending or by clip filename.")
    p_clips.add_argument("--accurate-video-bitrate", action="store_true", help="Sum video packet sizes for bitrate. Slower, but best for exact source Mbps.")
    services.add_bitrate_args(p_clips)
    services.add_postprocess_args(p_clips)
    services.add_makemkv_args(p_clips)
    p_clips.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_clips.add_argument("--verbose", action="store_true")
    p_clips.set_defaults(func=services.cmd_clips)

    p_convert = services.command_parser(sub, "convert", help="Convert a BD backup.", description="Legacy conversion command. For normal full-disc menu-preserving use, prefer 'auto', 'start', or 'queue'.", examples="""
  py bd2hevc.py convert "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc"
  py bd2hevc.py convert "BD backups\\Movie Disc" --mode clone-streams
  py bd2hevc.py convert "BD backups\\Movie Disc" --mode movie-only --title 0
""")
    services.add_convert_args(p_convert)
    p_convert.set_defaults(func=services.cmd_convert)

    p_auto = services.command_parser(sub, "auto", help="Faithful full-disc conversion. Only the source backup path is required.", description="Run a foreground full-disc conversion that preserves menus, extras, subtitles, and audio by default.", examples="""
  py bd2hevc.py auto "BD backups\\Movie Disc"
  py bd2hevc.py auto "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py auto "BD backups\\Movie Disc" --encoder libx265
  py bd2hevc.py auto "BD backups\\Movie Disc" --quality cq:20 --audio-mode compact-stereo
""")
    p_auto.add_argument("source", help="Source BD backup folder.")
    p_auto.add_argument("output", nargs="?", default=None, help="Output folder. Defaults to <source>_FULL_DISC_HEVC.")
    p_auto.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_auto.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_auto.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth. 8 preserves 8-bit BD sources and is VLC-friendly; use 10 for explicit Main10 output.")
    services.add_encoder_args(p_auto, include_encode_ahead=True)
    services.add_bitrate_args(p_auto)
    services.add_postprocess_args(p_auto)
    services.add_audio_args(p_auto)
    services.add_uhd_output_args(p_auto)
    services.add_iso_output_args(p_auto)
    p_auto.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_auto.add_argument("--progress-plan", default=None, help=services.argparse.SUPPRESS)
    p_auto.add_argument("--staging-dir", default=None)
    p_auto.add_argument("--keep-staging", action="store_true")
    p_auto.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    p_auto.add_argument("--dry-run", action="store_true")
    p_auto.add_argument("--no-progress", action="store_true", help="Do not print live conversion progress.")
    p_auto.add_argument("--report", default=None, help="Write the full JSON report to this path.")
    p_auto.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    services.add_makemkv_args(p_auto)
    p_auto.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_auto.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    services.add_vlc_compatibility_args(p_auto)
    p_auto.add_argument("--verbose", action="store_true")
    p_auto.set_defaults(func=services.cmd_auto)

    p_start = services.command_parser(sub, "start", help="Start a full-disc conversion in the background.", description="Start one background conversion job and return immediately with status commands.", examples="""
  py bd2hevc.py start "BD backups\\Movie Disc" --name Movie_Disc
  py bd2hevc.py start "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py start "BD backups\\Movie Disc" --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
""")
    p_start.add_argument("source", help="Source BD backup folder.")
    p_start.add_argument("output", nargs="?", default=None, help="Output folder. Defaults next to the source.")
    p_start.add_argument("--name", default=None, help="Friendly job id. Defaults to timestamp plus source folder name.")
    p_start.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_start.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_start.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    services.add_encoder_args(p_start, include_encode_ahead=True)
    services.add_bitrate_args(p_start)
    services.add_postprocess_args(p_start)
    services.add_audio_args(p_start)
    services.add_uhd_output_args(p_start)
    services.add_iso_output_args(p_start)
    p_start.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_start.add_argument("--force", action="store_true", help="Replace an existing output folder.")
    services.add_makemkv_args(p_start)
    p_start.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_start.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    services.add_vlc_compatibility_args(p_start)
    p_start.add_argument("--verbose", action="store_true")
    p_start.set_defaults(func=services.cmd_start)

    p_queue = services.command_parser(sub, "queue", help="Queue multiple full-disc conversions that run one at a time.", description="Queue one or more source folders. Jobs run one at a time in the background.", examples="""
  py bd2hevc.py queue "BD backups\\Movie Disc" --output-dir "Converted UHD-BD"
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD"
  py bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD" --encoder libx265
  py bd2hevc.py queue "Disc 1" "Disc 2" --output-dir "Converted UHD-BD" --quality cq:20
  py bd2hevc.py queue "Movie Disc" --output-dir "Converted UHD-BD" --quality cq:20 --main-title-quality cq:18 --audio-mode compact-stereo
  py bd2hevc.py queue "Episode Disc" --output-dir "Converted UHD-BD" --quality cq:20 --top-n-quality 3 cq:18
""")
    p_queue.add_argument("sources", nargs="+", help="Source BD backup folders or parent folders containing BDMV backups.")
    p_queue.add_argument("--output-dir", default=None, help="Put each converted output in this folder while preserving each source folder name.")
    output_tags = p_queue.add_mutually_exclusive_group()
    output_tags.add_argument("--output-tags", dest="add_output_tags", action="store_true", default=True, help="Append missing (BD) and (UHD converted) tags to generated output names (default).")
    output_tags.add_argument("--no-output-tags", dest="add_output_tags", action="store_false", help="Do not append format tags to generated output names.")
    p_queue.add_argument("--name-prefix", default=None, help="Prefix for generated job ids. Defaults to the current timestamp.")
    p_queue.add_argument("--fast-bitrate", action="store_true", help="Estimate video bitrate from container data instead of summing video packets.")
    p_queue.add_argument("--force-encode", action="store_true", help="Encode even when a video clip would normally be copied.")
    p_queue.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    services.add_encoder_args(p_queue, include_encode_ahead=True)
    services.add_bitrate_args(p_queue)
    services.add_postprocess_args(p_queue)
    services.add_audio_args(p_queue)
    services.add_uhd_output_args(p_queue)
    services.add_iso_output_args(p_queue)
    p_queue.add_argument("--decode-sample", type=float, default=30.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_queue.add_argument("--force", action="store_true", help="Replace existing output folders.")
    services.add_makemkv_args(p_queue)
    p_queue.add_argument("--no-patch-navigation", action="store_true", help="Do not update CLPI/MPLS stream descriptors from AVC to HEVC.")
    p_queue.add_argument("--no-bdj-compatibility-patches", action="store_true", help="Do not apply known disc-specific BD-J compatibility patches.")
    services.add_vlc_compatibility_args(p_queue)
    p_queue.add_argument("--verbose", action="store_true")
    p_queue.set_defaults(func=services.cmd_queue)

    p_status = services.command_parser(sub, "status", help="Show progress for a background conversion.", description="Show progress for the current job, a specific job, or the whole queue when watched without a job id.", examples="""
  py bd2hevc.py status
  py bd2hevc.py status --watch
  py bd2hevc.py status 20260528-My_Movie --watch
  py bd2hevc.py status 20260528-My_Movie --watch 5
""")
    p_status.add_argument("job", nargs="?", default=None, help="Job id, output folder, or source folder. Defaults to the newest job.")
    p_status.add_argument("--watch", nargs="?", const=1.0, type=float, default=0, help="Refresh every N seconds. Defaults to 1 second when no interval is supplied.")
    p_status.add_argument("--width", type=int, default=32)
    p_status.set_defaults(func=services.cmd_status)

    p_jobs = services.command_parser(sub, "jobs", help="List recent background conversions.", description="List running, queued, completed, failed, and canceled background jobs.", examples="""
  py bd2hevc.py jobs
  py bd2hevc.py jobs --limit 30
  py bd2hevc.py jobs --active
  py bd2hevc.py jobs --failed --hide-old-failed
""")
    p_jobs.add_argument("--limit", type=int, default=10)
    p_jobs.add_argument("--active", action="store_true", help="Show only running, queued, and paused jobs.")
    p_jobs.add_argument("--failed", action="store_true", help="Show only failed jobs.")
    p_jobs.add_argument("--completed", action="store_true", help="Show only completed jobs.")
    p_jobs.add_argument("--canceled", action="store_true", help="Show only canceled jobs.")
    p_jobs.add_argument("--hide-old-failed", action="store_true", help="Hide failed jobs when a newer completed job has the same output folder.")
    p_jobs.set_defaults(func=services.cmd_jobs)

    p_pause = services.command_parser(sub, "pause-queue", help="Pause the background queue after the current running job.", description="Pause queued jobs. The currently running conversion is allowed to continue.", examples="""
  py bd2hevc.py pause-queue
  py bd2hevc.py pause-queue --reason "Need the GPU for something else"
""")
    p_pause.add_argument("--reason", default=None, help="Optional note saved with the pause marker.")
    p_pause.set_defaults(func=services.cmd_pause_queue)

    p_resume = services.command_parser(sub, "resume-queue", help="Resume a paused background queue.", description="Resume jobs that were paused with pause-queue.", examples="""
  py bd2hevc.py resume-queue
""")
    p_resume.set_defaults(func=services.cmd_resume_queue)

    p_cancel = services.command_parser(sub, "cancel", help="Cancel a queued job. Use --kill to stop a running job.", description="Cancel a queued conversion. Use --kill only when you really want to stop a running conversion process.", examples="""
  py bd2hevc.py cancel 20260528-My_Movie
  py bd2hevc.py cancel "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py cancel 20260528-My_Movie --kill
""")
    p_cancel.add_argument("job", help="Job id, output folder, or source folder.")
    p_cancel.add_argument("--kill", action="store_true", help="Stop a running conversion process tree.")
    p_cancel.set_defaults(func=services.cmd_cancel_job)

    p_remove = services.command_parser(sub, "remove", help="Remove a job from the queue/status list without deleting converted output.", description="Hide an old job from BD2HEVC's job list. This does not delete the converted backup.", examples="""
  py bd2hevc.py remove 20260528-My_Movie
  py bd2hevc.py remove 20260528-My_Movie --kill
""")
    p_remove.add_argument("job", help="Job id, output folder, or source folder.")
    p_remove.add_argument("--kill", action="store_true", help="Allow removal of a running job by stopping it first.")
    p_remove.set_defaults(func=services.cmd_remove_job)

    p_validate = services.command_parser(sub, "validate", help="Validate an output clip or BDMV folder.", description="Run structural and decode checks against an output clip, converted backup, or source backup.", examples="""
  py bd2hevc.py validate "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py validate "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --reference "BD backups\\Movie Disc"
  py bd2hevc.py validate "BD backups\\Movie Disc" --source-backup
""")
    p_validate.add_argument("target")
    p_validate.add_argument("--source-backup", action="store_true", help="Validate a source BD backup without requiring HEVC output clips.")
    p_validate.add_argument("--reference", default=None, help="Original BD backup folder to compare matching stream audio and timestamps against.")
    p_validate.add_argument("--audio-mode", choices=services.AUDIO_MODES, default=services.DEFAULT_AUDIO_MODE, help="Expected audio handling when comparing against --reference. Use compact-stereo for AC-3 stereo/mono outputs.")
    p_validate.add_argument("--decode-sample", type=float, default=None, help="Decode the first N seconds of video to null.")
    p_validate.add_argument("--report", default=None, help="Write the full JSON validation report to this path.")
    p_validate.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    services.add_makemkv_args(p_validate)
    p_validate.add_argument("--verbose", action="store_true")
    p_validate.set_defaults(func=services.cmd_validate)

    p_diagnose = services.command_parser(sub, "diagnose", help="Create a redacted support bundle.", description="Create a shareable diagnostic zip with redacted logs, tool versions, validation output, and file manifests. Media files and raw disc assets are not included.", examples="""
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --source "BD backups\\Movie Disc"
  py bd2hevc.py diagnose "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --job 20260429-153012-Movie_Disc
""")
    p_diagnose.add_argument("target", help="Converted output folder, source backup folder, or clip to summarize.")
    p_diagnose.add_argument("--source", default=None, help="Original source backup for reference validation and comparison.")
    p_diagnose.add_argument("--job", default=None, help="Matching background job id or prefix when auto-detection is not enough.")
    p_diagnose.add_argument("--output", default=None, help="Destination zip or folder. Defaults to reports/diagnostics/<disc>-<timestamp>.zip.")
    p_diagnose.add_argument("--log-lines", type=int, default=services.DEFAULT_DIAGNOSTIC_LOG_LINES, help=f"Number of job log lines to include from the end of the log. Default {services.DEFAULT_DIAGNOSTIC_LOG_LINES}.")
    p_diagnose.add_argument("--no-validation", action="store_true", help="Skip the lightweight no-MakeMKV validation pass.")
    p_diagnose.add_argument("--force", action="store_true", help="Replace an existing diagnostic bundle; media remains protected.")
    p_diagnose.add_argument("--no-zip", action="store_true", help="Write an unpacked diagnostic folder instead of a zip file.")
    p_diagnose.add_argument("--json", action="store_true", help="Print machine-readable command output.")
    p_diagnose.set_defaults(func=services.cmd_diagnose)

    p_play = services.command_parser(sub, "play", help="Open a BD/UHD-BD backup in VLC with clean BD-J menu startup.", description="Launch VLC as a fresh Blu-ray menu session. This avoids VLC resume prompts, playlist enqueueing, and existing-instance reuse, which can upset some BD-J menus.", examples="""
  py bd2hevc.py play "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py play "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --region A
  py bd2hevc.py play "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --dry-run
""")
    p_play.add_argument("target", help="Converted output folder, source backup folder, or disc folder to open in VLC.")
    p_play.add_argument("--region", choices=["A", "B", "C", "a", "b", "c"], default=None, help="Pass a Blu-ray region to VLC for this launch.")
    p_play.add_argument("--no-bdj-persistent-storage", action="store_true", help="Disable libbluray BD-J persistent storage for this launch.")
    p_play.add_argument("--dry-run", action="store_true", help="Print the VLC command without opening VLC.")
    p_play.add_argument("--json", action="store_true", help="Print machine-readable command output.")
    p_play.set_defaults(func=services.cmd_play)

    p_record = services.command_parser(sub, "record-libbluray", help="Record an interactive VLC/libbluray reproduction session.", description="Open VLC visibly, let you reproduce a menu/gallery failure, then package the verbose libbluray log and safe disc metadata into a support bundle.", examples="""
  py bd2hevc.py record-libbluray "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py record-libbluray "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --source "BD backups\\Movie Disc" --label movie-gallery
  py bd2hevc.py record-libbluray "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --region A --duration 120
  py bd2hevc.py record-libbluray "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --isolated-bdj-storage
  py bd2hevc.py record-libbluray "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --libbluray-debug-mask
""")
    p_record.add_argument("target", help="Converted output folder, source backup folder, or disc folder to open in VLC.")
    p_record.add_argument("--source", default=None, help="Original source backup for reference file manifests.")
    p_record.add_argument("--label", default=None, help="Short name for the recording bundle. Defaults to the disc folder name.")
    p_record.add_argument("--output-dir", default=None, help="Folder for recording bundles. Defaults to reports/libbluray-recordings.")
    p_record.add_argument("--region", choices=["A", "B", "C", "a", "b", "c"], default=None, help="Pass a Blu-ray region to VLC for this recording.")
    p_record.add_argument("--duration", type=float, default=None, help="Automatically stop after N seconds instead of waiting for Enter.")
    p_record.add_argument("--verbose-level", type=int, default=3, choices=[0, 1, 2, 3, 4], help="VLC verbosity level. Default 3 for libbluray debugging.")
    p_record.add_argument("--isolated-bdj-storage", action="store_true", help="Run VLC with per-recording libbluray BD-J cache and persistent storage roots.")
    p_record.add_argument("--libbluray-debug-mask", nargs="?", const="0x3e940", default=None, help="Also set BD_DEBUG_FILE and BD_DEBUG_MASK for a direct libbluray log. With no value, captures CRIT, BluRay, NAV, BD-J, stream, graphics, decode, and JNI categories.")
    p_record.add_argument("--no-zip", action="store_true", help="Write an unpacked recording folder instead of a zip.")
    p_record.add_argument("--dry-run", action="store_true", help="Show the VLC command and bundle path without opening VLC.")
    p_record.add_argument("--json", action="store_true", help="Print machine-readable command output.")
    p_record.set_defaults(func=services.cmd_record_libbluray)

    p_playlist = services.command_parser(sub, "playlist-probe", help="Probe a Blu-ray playlist through libbluray/FFprobe and fail on stale CLPI packet maps.", description="Probe one MPLS playlist through libbluray/FFprobe, useful when VLC progress or seeking looks wrong.", examples="""
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23 --reference "BD backups\\Movie Disc"
  py bd2hevc.py playlist-probe "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --playlist 23 --count-frames --decode-seconds 30
""")
    p_playlist.add_argument("target", help="BD/UHD-BD backup folder.")
    p_playlist.add_argument("--playlist", type=int, required=True, help="MPLS playlist number, e.g. 23 for 00023.mpls.")
    p_playlist.add_argument("--reference", default=None, help="Original BD backup folder to compare playlist duration against.")
    p_playlist.add_argument("--reference-tolerance", type=float, default=2.0, help="Allowed duration difference from --reference, in seconds.")
    p_playlist.add_argument("--min-duration", type=float, default=None)
    p_playlist.add_argument("--max-duration", type=float, default=None)
    p_playlist.add_argument("--count-frames", action="store_true", help="Ask FFprobe to count decoded video frames.")
    p_playlist.add_argument("--min-video-frames", type=int, default=None, help="Require at least this many decoded video frames.")
    p_playlist.add_argument("--decode-seconds", type=float, default=None, help="Decode this many seconds of playlist video with FFmpeg.")
    p_playlist.add_argument("--allow-eof", action="store_true", help="Do not fail when libbluray reports Read past EOF.")
    p_playlist.add_argument("--report", default=None, help="Optional JSON report path.")
    p_playlist.set_defaults(func=services.cmd_playlist_probe)

    p_metadata = services.command_parser(sub, "patch-disc-metadata", help="Create fallback BD disc-library metadata when a backup is missing it.", description="Create simple BD disc-library metadata so VLC shows a disc title instead of a file URL.", examples="""
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD" --force
  py bd2hevc.py patch-disc-metadata "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --title "Movie Disc"
""")
    p_metadata.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_metadata.add_argument("--title", default=None, help="Use this title for every patched disc. Defaults to a cleaned folder name.")
    p_metadata.add_argument("--force", action="store_true", help="Overwrite existing bdmt_*.xml metadata.")
    p_metadata.set_defaults(func=services.cmd_patch_disc_metadata)

    p_uhd_profile = services.command_parser(sub, "patch-uhd-profile", help="Patch an existing output toward UHD-BD folder conventions.", description="Create expected UHD-BD-style folders and mirror required backup files. Library mode restores BD-style navigation headers for VLC compatibility; disc mode patches headers toward UHD.", examples="""
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD"
  py bd2hevc.py patch-uhd-profile "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --uhd-profile disc --json
""")
    p_uhd_profile.add_argument("paths", nargs="+", help="Disc folders or a parent folder containing disc folders.")
    p_uhd_profile.add_argument("--uhd-profile", choices=["library", "disc", "auto", "off"], default="library", help="library restores BD-style 0200 headers; disc patches headers toward 0300 UHD-style values.")
    p_uhd_profile.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_uhd_profile.set_defaults(func=services.cmd_patch_uhd_profile)

    p_patch = services.command_parser(sub, "patch-navigation", help="Patch full-disc CLPI/MPLS descriptors for HEVC replacement clips.", description="Patch Blu-ray navigation metadata after HEVC replacement so players see the new video streams correctly.", examples="""
  py bd2hevc.py patch-navigation "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --reference "BD backups\\Movie Disc"
  py bd2hevc.py patch-navigation "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 00002
""")
    p_patch.add_argument("target")
    p_patch.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to mark as HEVC. Defaults to HEVC clips over 10 seconds.")
    p_patch.add_argument("--reference", default=None, help="Original BD backup. When supplied, source CLPI files are restored, patched to HEVC, and their CPI packet maps are scaled to the output streams.")
    p_patch.add_argument("--refresh-cpi", action="store_true", default=False, help="Experimental: splice tsMuxer-generated CPI blocks into CLPI files. Normally leave this off.")
    p_patch.add_argument("--no-refresh-cpi", action="store_false", dest="refresh_cpi", help=services.argparse.SUPPRESS)
    p_patch.add_argument("--uhd-profile", choices=["library", "disc", "auto", "off"], default="library", help="library keeps BD-style navigation version headers; disc patches CLPI/MPLS version headers toward UHD.")
    p_patch.add_argument("--verbose", action="store_true")
    p_patch.set_defaults(func=services.cmd_patch_navigation)

    p_remux = services.command_parser(sub, "remux-replacements", help="Remux existing HEVC replacement clips with the current converter M2TS authoring rules.", description="Rebuild replacement M2TS files without reencoding their existing HEVC video.", examples="""
  py bd2hevc.py remux-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py remux-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 00002
""")
    p_remux.add_argument("source", help="Original BD backup folder.")
    p_remux.add_argument("output", help="Converted full-disc output folder.")
    p_remux.add_argument("--clips", nargs="*", default=None, help="Clip filenames or ids to remux. Defaults to HEVC clips over 10 seconds.")
    p_remux.add_argument("--verbose", action="store_true")
    p_remux.set_defaults(func=services.cmd_remux_replacements)

    p_reencode = services.command_parser(sub, "reencode-replacements", help="Reencode selected replacement clips in an existing full-disc output.", description="Reencode selected clips in an existing converted output, then remux and repatch navigation.", examples="""
  py bd2hevc.py reencode-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001
  py bd2hevc.py reencode-replacements "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001 --bitrate-mode compact-cq --compact-cq-value 20
""")
    p_reencode.add_argument("source", help="Original BD backup folder.")
    p_reencode.add_argument("output", help="Converted full-disc output folder.")
    p_reencode.add_argument("--clips", nargs="+", required=True, help="Clip filenames or ids to reencode.")
    p_reencode.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="HEVC output bit depth.")
    services.add_encoder_args(p_reencode)
    services.add_bitrate_args(p_reencode)
    p_reencode.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each reencoded output clip during validation. Use 0 to skip.")
    p_reencode.add_argument("--verbose", action="store_true")
    p_reencode.set_defaults(func=services.cmd_reencode_replacements)

    p_audio_repair = services.command_parser(sub, "repair-compact-audio", help="Convert non-compact audio in an existing full-disc output to AC-3 without reencoding video.", description="Repair an existing converted backup in place. Video and subtitles are stream-copied, playable audio is converted to compact AC-3 mono/stereo, CLPI/MPLS metadata is updated, and each clip rolls back if validation fails. The original source backup is not required.", examples=r"""
  py bd2hevc.py repair-compact-audio "Converted UHD-BD\Movie Disc (BD) (UHD converted)" --dry-run
  py bd2hevc.py repair-compact-audio "Converted UHD-BD\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py repair-compact-audio "Converted UHD-BD\Movie Disc (BD) (UHD converted)" --clips 00004 00012 --require-makemkv
""")
    p_audio_repair.add_argument("target", help="Existing converted BD/UHD-BD backup folder to repair in place.")
    p_audio_repair.add_argument("--clips", nargs="*", default=None, help="Optional clip filenames or ids to inspect. Defaults to every M2TS clip and repairs only audio that does not already match the compact target.")
    p_audio_repair.add_argument("--stereo-audio-bitrate", type=services.parse_bitrate_arg, default=services.DEFAULT_STEREO_AUDIO_BITRATE, help="Bitrate for two-channel AC-3 audio. Default 256k.")
    p_audio_repair.add_argument("--mono-audio-bitrate", type=services.parse_bitrate_arg, default=services.DEFAULT_MONO_AUDIO_BITRATE, help="Bitrate for mono AC-3 audio. Default 128k.")
    p_audio_repair.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each repaired output clip during validation. Use 0 to skip.")
    p_audio_repair.add_argument("--uhd-profile", choices=["library", "disc", "auto", "off"], default="library", help="library keeps BD-style navigation headers; disc patches repaired navigation headers toward UHD-style versions.")
    services.add_makemkv_args(p_audio_repair)
    p_audio_repair.add_argument("--dry-run", action="store_true", help="Show clips that need compact-audio repair without changing files.")
    p_audio_repair.add_argument("--report", default=None, help="Write the full JSON plan or repair report to this path.")
    p_audio_repair.add_argument("--json", action="store_true", help="Print the full JSON plan or repair report.")
    p_audio_repair.add_argument("--verbose", action="store_true")
    p_audio_repair.set_defaults(func=services.cmd_repair_compact_audio)

    p_repair = services.command_parser(sub, "repair-output", help="Automatically repair an existing converted full-disc output.", description="Inspect and repair an existing converted backup using the current replacement and navigation rules.", examples="""
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --dry-run
  py bd2hevc.py repair-output "BD backups\\Movie Disc" "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --clips 00001
""")
    p_repair.add_argument("source", help="Original BD backup folder.")
    p_repair.add_argument("output", help="Converted full-disc output folder.")
    p_repair.add_argument("--clips", nargs="*", default=None, help="Optional clip filenames or ids to force-reencode. Defaults to wrong-bit-depth replacements.")
    p_repair.add_argument("--hevc-bit-depth", type=int, choices=[8, 10], default=8, help="Desired HEVC output bit depth.")
    services.add_encoder_args(p_repair)
    services.add_bitrate_args(p_repair)
    p_repair.add_argument("--decode-sample", type=float, default=10.0, help="Decode N seconds of each repaired output clip during validation. Use 0 to skip.")
    p_repair.add_argument("--dry-run", action="store_true")
    p_repair.add_argument("--json", action="store_true", help="Print the full JSON report instead of a short summary.")
    p_repair.add_argument("--verbose", action="store_true")
    p_repair.set_defaults(func=services.cmd_repair_output)

    p_vlc_patch = services.command_parser(sub, "patch-vlc-compat", help="Apply modular VLC/libbluray compatibility fixes to an existing output.", description="Apply optional BD-J compatibility patches to an already converted backup.", examples="""
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --vlc-fix topmenu-mark-zero-on-return
  py bd2hevc.py patch-vlc-compat "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --vlc-compat off
""")
    p_vlc_patch.add_argument("target", help="BD/UHD-BD backup folder.")
    services.add_vlc_compatibility_args(p_vlc_patch)
    p_vlc_patch.add_argument("--json", action="store_true", help="Print the full JSON report.")
    p_vlc_patch.set_defaults(func=services.cmd_patch_vlc_compat)

    p_vlc = services.command_parser(sub, "vlc-smoke", help="Headless VLC/libbluray startup smoke test; does not open a visible video window.", description="Run a short VLC startup test against a BD/UHD-BD backup without opening a visible VLC window.", examples="""
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)"
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --seconds 45 --video-plane
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --video-plane --isolated-bdj-storage
  py bd2hevc.py vlc-smoke "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --d3d11
""")
    p_vlc.add_argument("target", help="BD/UHD-BD backup folder.")
    p_vlc.add_argument("--seconds", type=float, default=35.0, help="How long VLC should run before exiting. Default 35 seconds for slower BD-J startup screens.")
    p_vlc.add_argument("--log", default=None, help="VLC log path. Defaults to reports/<disc>.vlc_headless_smoke.log.")
    p_vlc.add_argument("--video-plane", action="store_true", help="Use VLC dummy video output instead of --no-video, exercising video/subpicture paths without opening a visible window.")
    p_vlc.add_argument("--d3d11", action="store_true", help="Force VLC's D3D11VA decoder path and fail on known D3D11 video-freeze warnings.")
    p_vlc.add_argument("--allow-resume", action="store_true", help="Allow VLC to resume remembered playback state instead of forcing menu startup.")
    p_vlc.add_argument("--region", choices=["A", "B", "C", "a", "b", "c"], default=None, help="Pass a Blu-ray region to VLC for this smoke test.")
    p_vlc.add_argument("--isolated-bdj-storage", action="store_true", help="Run VLC with a fresh libbluray BD-J cache/persistent-storage root for this smoke test.")
    p_vlc.add_argument("--no-bdj-persistent-storage", action="store_true", help="Disable libbluray BD-J persistent storage for this smoke test.")
    p_vlc.add_argument("--verbose", action="store_true")
    p_vlc.set_defaults(func=services.cmd_vlc_smoke)

    p_progress = services.command_parser(sub, "progress", help="Show a progress bar for a running full-disc conversion.", description="Low-level progress command used by older workflows. For background jobs, prefer 'status --watch'.", examples="""
  py bd2hevc.py progress "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --plan reports\\jobs\\job.plan.json
  py bd2hevc.py progress "Converted UHD-BD\\Movie Disc (BD) (UHD converted)" --plan reports\\jobs\\job.plan.json --log reports\\jobs\\job.log --watch
""")
    p_progress.add_argument("target", help="Output BD folder being written.")
    p_progress.add_argument("--plan", required=True, help="Dry-run JSON produced before the matching conversion.")
    p_progress.add_argument("--log", default=None, help="Optional conversion log for current-clip progress.")
    p_progress.add_argument("--width", type=int, default=32)
    p_progress.add_argument("--watch", nargs="?", const=1.0, type=float, default=0, help="Refresh every N seconds until stopped. Defaults to 1 second when no interval is supplied.")
    p_progress.set_defaults(func=services.cmd_progress)
    return parser
