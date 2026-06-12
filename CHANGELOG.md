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
- Added optional metadata-driven deinterlacing for reencoded clips, with
  `--deinterlace auto`, manual clip include/exclude flags, and preset support.
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
- Added a `play` command for opening backups in VLC as fresh Blu-ray-menu
  sessions, avoiding VLC resume prompts and existing-instance playlist reuse
  that can upset some BD-J startup screens.
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
- Added optional direct libbluray debug capture to `record-libbluray` via
  `--libbluray-debug-mask`.
- Added modular VLC/libbluray compatibility fixes with `--vlc-compat`,
  repeatable `--vlc-fix`, JSON `--compat-patch-file` support, and
  `patch-vlc-compat` for existing outputs.
- Added the `music-jukebox-queued-state` VLC compatibility fix for matching
  Warner-style BD-J music jukebox menus, including faithful previous-menu
  cleanup, authored track-group layering for matching extracted menu resources,
  and null-focus recovery before the disc's original playback helper runs.
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
- Added a hardware compact-audio dependency pipeline with independent video and
  audio lanes, feeding a single mux lane once each clip's pair of outputs is
  ready.
- Added `--top-n-cq COUNT CQ` for episode discs, letting the longest reencoded
  CQ clips use a different CQ from extras; mutually exclusive with
  `--main-title-cq`.
- Added unified quality selectors: `--quality`, `--main-title-quality`,
  `--top-n-quality`, and repeatable `--clip-quality`, with support for bitrate
  presets, explicit `source-ratio:N` factors, legacy compact presets, `cq:N`,
  and `copy`/`no-reencode`.
- Added `clips` command to list M2TS clip ids, durations, codecs, source
  bitrates, and planned quality so users can choose clip-specific overrides
  without reading raw JSON.
- `clips` now separates source codec from planned output codec so copied source
  clips and HEVC replacements are easy to distinguish.
- Added `--copy-clips` / `--exclude-clips` for preserving selected source clips
  untouched when a menu, game, or authored still-video clip should not be
  reencoded.
- Added codec-specific source-ratio overrides through repeatable
  `--codec-source-ratio CODEC=FACTOR` and JSON preset
  `codec_source_ratios`, so mixed AVC/MPEG-2/VC-1 discs can use different
  HEVC/source multipliers while keeping one general preset.
- Added `--preset-file` as a shorter alias for `--bitrate-preset-file`.
- Added named preset management with `preset save/list/show/remove` and
  `--preset NAME`, so common quality/audio profiles can be created, reused, and
  removed without manually editing or remembering JSON file paths.
- Added an always-on UHD-like structure pass that creates expected
  BDMV/CERTIFICATE folder placeholders and mirrors required backup navigation
  files. Library mode keeps/restores BD-style navigation version headers for
  VLC/libbluray compatibility, while `--uhd-profile disc` patches headers
  toward UHD-BD conventions. Existing outputs can be updated with
  `patch-uhd-profile`.
- Added isolated libbluray BD-J cache/persistent-storage options to
  `vlc-smoke` and `record-libbluray`, plus clearer smoke-test diagnostics for
  discs that open BD-J but never hand off to playlist streams.
- Changed `--uhd-profile` into an intent/guardrail selector: `library` is the
  normal digital-library default, while `disc` requires a target disc size and
  predictable VBR quality.
- Added `--target-disc-size` and `--target-disc-margin` so VBR conversions can
  scale planned replacement-video bitrates toward BD-25/BD-50/BD-66/BD-100 or
  explicit byte budgets.
- Accurate bitrate planning now subtracts safe coded padding for normal library
  conversions: AVC/H.264 filler NAL units, HEVC/H.265 filler NAL units, and
  VC-1 stuffing bytes. `--keep-source-padding` restores the previous padded
  source estimate for comparisons, while `--uhd-profile disc` keeps padded
  totals for conservative physical-disc sizing.
- Added `scripts/source_padding_audit.py`, a read-only helper for locating
  transport null packets and safe coded-video padding in source BD backups.
- Compact stereo audio now skips unplayable zero-channel streams that FFprobe
  can misidentify as audio, and maps source audio by stream index.
- Improved generated output names with acronym and Roman numeral preservation,
  backed by CC0 acronym data plus media-specific terms.
- Improved sparse silent menu/gallery clip detection so still-like video-only
  clips preserve their real source timing after conversion.
- Improved CLPI CPI repair for short repeated-playitem menu/gallery clips by
  mapping restored source entries to the actual HEVC keyframe packet positions,
  avoiding ratio-scaled packet maps that can truncate gallery wrapper
  playlists.
- Added `record-libbluray`, an interactive VLC/libbluray debug recorder that
  captures a menu/gallery reproduction log plus safe manifests for support
  reports without bundling media or raw disc assets.
- Restricted navigation version-header patching to `--uhd-profile disc`; normal
  library conversions now keep or restore source-era BD headers to avoid VLC
  BD-J startup stalls on fragile discs.
