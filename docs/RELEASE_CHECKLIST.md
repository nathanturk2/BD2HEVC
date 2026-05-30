# Release Checklist

Use this before publishing BD2HEVC.

## Current Readiness

The codebase is divided enough for an initial open-source release. Low-level
disc handling now lives in focused modules for BD-J compatibility patches,
bitrate planning, encoding, muxing, navigation metadata, output repair,
progress rendering, queueing, scanning, tool discovery, validation, and output
management. `bd2hevc_app.core` remains as high-level conversion orchestration,
command wrappers, queue planning, and parser dispatch.

Further splitting is optional polish. It may be useful later if contributors
want to work independently on CLI command construction or conversion workflow
orchestration, but it should not block `v0.1.0`.

## Repository

- Initialize git if this folder is not already a repository.
- Confirm source backups, converted discs, logs, external binaries, and reports
  are ignored by `.gitignore`.
- Keep the included GitHub Actions CI workflow and issue templates unless you
  prefer to customize them before the first push.
- Pick a public version tag, for example `v0.1.0`.
- Confirm the public release folder does not contain `__pycache__`, reports,
  logs, source backups, converted outputs, private paths, or external binaries.

## Local Checks

Run from the project root:

```bash
python -m py_compile bd2hevc_app/*.py bd_to_uhdbd.py bd2hevc.py
python -m unittest discover -s tests
python bd2hevc.py --help
python bd2hevc.py auto --help
python bd2hevc.py start --help
python bd2hevc.py queue --help
python bd2hevc.py status --help
python bd2hevc.py tools
```

Run at least one dry run:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP" --dry-run --no-makemkv --decode-sample 0
```

Confirm the dry run prints a short human summary. Save detailed reports with
`--report` when needed:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP" --dry-run --no-makemkv --report reports/my_disc.plan.json
```

Validate a converted output:

```bash
python bd2hevc.py validate "Converted UHD-BD/My Disc (BD) (UHD converted)" --reference "MY_DISC_BACKUP" --no-makemkv --decode-sample 0
```

## Suggested First Release Notes

- Full-disc Blu-ray backup conversion to HEVC.
- Friendly foreground and background workflows: `auto`, `start`, `status`, and
  `jobs`.
- Modular internals for bitrate planning, encoding, muxing, navigation metadata,
  BD-J/VLC compatibility, queueing, progress rendering, validation, and repair.
- FIFO background queue plus a `queue` command for multiple conversions.
- Short human summaries by default, with JSON available through `--json` and
  `--report`.
- Preserves menus, extras, playlists, audio, subtitles, and BD-J files.
- Reencodes clips longer than 10 seconds to HEVC. NVENC is the tested default,
  with QSV, AMF, and `libx265` selectable when FFmpeg supports them.
- Preserves sparse menu/gallery clip timing during HEVC replacement.
- Uses no-B-frame HEVC for sparse menu/gallery replacements to ease VLC/D3D11
  playback.
- Keeps original resolution; no upscaling in the default workflow.
- Optional MakeMKV validation.
- VLC/libbluray smoke-test helper.
- Adjustable HEVC bitrate presets and manual bitrate controls.
- `compact-cq` preset for compact CQ storage on multi-episode/anime discs and
  high-bitrate movie discs.
- Windows tested; WSL/Linux unit tests pass, with native POSIX tool discovery
  that avoids accidental Windows `.exe` tools.

## Optional Pre-Announcement Polish

These are useful but not release blockers:

- Add screenshots or terminal captures of `auto`, `start`, `status --watch`,
  and `validate`.
- Run one fresh end-to-end conversion from the release folder itself.
- Try a Linux dry run with native PATH-based `ffmpeg`, `ffprobe`, and `tsmuxer`.
- Open a few starter GitHub issues tagged `help wanted`, for example Linux
  testing, non-NVIDIA encoder testing, and additional BD-J compatibility
  reports.
- Add a short security/scope note to GitHub Discussions or the README if users
  begin asking for decryption/ripping support.

## Reddit Or Forum Post Notes

Be explicit about scope:

- It works on already-decrypted local Blu-ray backups only.
- It does not provide decryption, keys, discs, BD-J source, or copyrighted data.
- It is aimed at preserving the full-disc menu experience while reducing video
  size with HEVC.
- Ask testers to include the disc title, OS, GPU, FFmpeg version, tsMuxer
  version, command used, and validation output when reporting issues.
