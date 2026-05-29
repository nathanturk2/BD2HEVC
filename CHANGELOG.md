# Changelog

## 0.1.0 - Unreleased

- Prepared initial public release metadata and GPL-3.0-only licensing.
- Documented Windows and Linux platform support, including WSL with native
  Linux media tools.
- Added `--version`.
- Added BD2HEVC command-line launcher and package metadata.
- Added GitHub Actions CI and issue templates for bug and compatibility
  reports.
- Split the codebase into focused modules for BD-J compatibility patching,
  bitrate planning, encoding, muxing, navigation metadata, output repair,
  progress rendering, queueing, scanning, tool discovery, validation, and output
  handling.
- Added user-friendly `start`, `status`, and `jobs` commands for background
  conversions without manual dry-run/log/progress command juggling.
- Made status watch rendering terminal-width aware so long disc names and log
  paths do not wrap and leave duplicate progress lines in Windows terminals.
- Background jobs now run through a FIFO queue, and `queue` can enqueue
  multiple conversions from one command line.
- Changed common commands to print short human summaries by default, with
  `--json` and `--report` available for detailed machine-readable reports.
- Added faithful full-disc HEVC conversion preserving menus, extras, audio,
  subtitles, playlists, and BD-J files.
- Added automatic foreground progress output for full-disc conversions.
- Added adjustable HEVC bitrate presets and manual bitrate controls.
- Added MPEG-2-aware HEVC bitrate estimation so MPEG-2 clips selected for
  reencoding do not inherit bogus Blu-ray CPB ceiling bitrates.
- Added sparse menu/gallery timing detection so long still-like clips are
  expanded to their source duration instead of collapsing to decoded frames.
- Sparse menu/gallery replacements now use no-B-frame HEVC to reduce VLC/D3D11
  hardware-decoder allocation failures during rapid gallery navigation.
- Made MakeMKV optional for full-disc conversion and validation, with
  `--require-makemkv` for stricter runs.
- Added Linux-friendly tool discovery for native PATH-based `ffmpeg`,
  `ffprobe`, `tsmuxer`, `makemkvcon`, and `vlc`, including WSL handling that
  avoids accidental Windows `.exe` tools.
- Added POSIX-safe background queue detachment and cancellation behavior.
- Added VLC smoke-test helper that avoids saved resume/bookmark state by
  default.
- Added validation checks for long-clip HEVC output, audio passthrough,
  source-aligned timestamps, sparse clip timing preservation, and no-B-frame
  sparse HEVC output.
- Added fallback BD disc-library metadata generation and a
  `patch-disc-metadata` command for existing converted backups.
- Added CLPI CPI packet-map scaling for HEVC replacements so VLC title progress
  and seeking no longer follow stale AVC packet positions past the smaller HEVC
  streams.
- Added `playlist-probe` to validate specific Blu-ray playlists through
  libbluray/FFprobe, including duration, video frame count, decode samples, and
  stale-CLPI `Read past EOF` detection.
- Added modular VLC/libbluray compatibility fixes with `--vlc-compat`,
  repeatable `--vlc-fix`, JSON `--compat-patch-file` support, and
  `patch-vlc-compat` for existing outputs.
- Generalized the validated BD-J top-menu compatibility fix so auto mode
  applies it only when the matching BD-J wrapper signature is present.
- Added compact audio mode with AC-3 stereo/mono output for storage-limited
  conversions, plus validation checks for compact audio stream count, codecs,
  and channel layout.
- Added `--main-title-cq` so compact CQ profiles can use a higher-quality CQ
  for the longest movie/title while keeping extras at the general CQ.
- Improved compact CQ behavior so it directly uses the requested CQ for all
  reencoded clips at or above the duration threshold.
- Improved progress reporting with separate encoding, audio, and muxing lanes,
  one-second FFmpeg progress updates, and carriage-return FFmpeg stat parsing
  so watch output no longer appears frozen during long clips.
- Made queueing faster and cleaner by moving full plan generation into the
  background job instead of creating a foreground placeholder plan.
- Hardened queue job-file writes and reads against transient empty JSON files
  and Windows file-replacement races while status is watching a running queue.
- Clarified queued job status so later jobs show how many jobs are ahead
  instead of appearing to wait only behind the current running job.
- Added command-specific CLI help examples, with top-level `--help` pointing
  users to `py bd2hevc.py <command> --help`.
- Added a hardware encode-ahead compact-audio pipeline so compact stereo audio
  for the next clip can be transcoded while the previous clip is muxing.
- Added `--top-n-cq COUNT CQ` for episode discs, letting the longest reencoded
  CQ clips use a different CQ from extras; mutually exclusive with
  `--main-title-cq`.
- Compact stereo audio now skips unplayable zero-channel streams that FFprobe
  can misidentify as audio, and maps source audio by stream index.
- Improved generated output names with acronym and Roman numeral preservation,
  backed by CC0 acronym data plus media-specific terms.
- Improved sparse silent menu/gallery clip detection so still-like video-only
  clips preserve their real source timing after conversion.
