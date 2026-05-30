# BD2HEVC

BD2HEVC converts local, unencrypted Blu-ray folder backups to HEVC while keeping
the full-disc experience: menus, extras, playlists, BD-J files, audio, subtitles,
chapters, and navigation metadata.

The goal is a smaller backup that still behaves like the original disc in
VLC/libbluray. BD2HEVC does not decrypt discs, does not include keys, and does
not upscale video. The normal full-disc workflow keeps 1080p sources at 1080p
and reencodes non-HEVC video clips longer than 10 seconds to 8-bit HEVC Main.

License: GPL-3.0-only.

Supported platforms: Windows and Linux. WSL is supported for Linux-side
conversion when native Linux `ffmpeg`, `ffprobe`, and `tsmuxer` are installed on
the WSL `PATH`.

BD2HEVC is a reencoding and preservation tool for backups you already have. It
does not decrypt discs, bypass copy protection, provide keys, download media, or
include copyrighted disc assets.

Project status: alpha. The current structure is ready for initial public
testing, with low-level encoding, muxing, navigation metadata, BD-J
compatibility, queueing, validation, and repair code split into focused modules.
Disc compatibility should still be treated as community-tested rather than
guaranteed.

## Table Of Contents

- [Scope At A Glance](#scope-at-a-glance)
- [Quick Start](#quick-start)
- [Background Jobs](#background-jobs)
- [Quality And Audio Recipes](#quality-and-audio-recipes)
- [VLC Compatibility Fixes](#vlc-compatibility-fixes)
- [Normal Validation](#normal-validation)
- [Automated Support Reports](#automated-support-reports)
- [Requirements](#requirements)
- [Installing Tools](#installing-tools)
- [VLC Java Setup For Menus](#vlc-java-setup-for-menus)
- [What It Changes](#what-it-changes)
- [Bitrate Controls](#bitrate-controls)
- [Supported So Far](#supported-so-far)
- [Repair And Diagnostics](#repair-and-diagnostics)
- [JSON Output](#json-output)
- [Legal](#legal)

## Scope At A Glance

- Input: existing local `BDMV` folder backups that are already accessible to
  normal media tools.
- Output: a smaller full-disc folder backup with menus, extras, playlists,
  BD-J, chapters, subtitles, and navigation metadata preserved.
- Video: non-HEVC clips longer than 10 seconds are reencoded to HEVC/H.265; the
  default workflow keeps the source resolution and does not upscale.
- Audio: passed through by default, with optional compact AC-3 stereo/mono for
  storage-limited collections.
- Not included: decryption, keys, ripping from protected discs, downloads, or
  copyrighted disc assets.

## Quick Start

Check that BD2HEVC can see the tools it needs:

```bash
python bd2hevc.py tools
```

Every command has built-in help with examples:

```bash
python bd2hevc.py --help
python bd2hevc.py queue --help
python bd2hevc.py diagnose --help
```

Convert in the foreground:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP"
```

Convert to a specific collection folder:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP" "Converted UHD-BD/My Disc (BD) (UHD converted)"
```

Preview what would be reencoded without writing an output:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP" --dry-run
```

The foreground command prints progress automatically and ends with a short
summary such as the output path, number of clips reencoded, and validation
status. Use `--report reports/name.json` when you want the full machine-readable
JSON report saved as well.

BD2HEVC targets HEVC/H.265 regardless of encoder. The tested default is
`--encoder hevc_nvenc`, and you can also select `hevc_qsv`, `hevc_amf`, or
`libx265` when your FFmpeg build supports them. If NVENC is unavailable, rerun
with an explicit alternative encoder, for example:

```bash
python bd2hevc.py auto "MY_DISC_BACKUP" --encoder libx265
python bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD" --encoder hevc_qsv
```

Full-disc conversion uses a small encode-to-mux queue for hardware HEVC
encoders: one clip encodes while the single muxer finishes earlier clips. When
`--audio-mode compact-stereo` is also enabled, compact audio gets its own middle
stage so audio for the next clip can transcode while the previous clip is
muxing. CPU `libx265` stays serial. Use `--no-encode-ahead` to disable that
pipeline even with hardware encoding, or `--encode-ahead-depth 1` to allow only
one completed encode to wait for later stages.

## Background Jobs

For long conversions, use `start`. It creates the dry-run plan, queues the real
conversion in the background, and prints the exact status commands to use.
Background jobs run one at a time so multiple conversions do not fight over the
same encoder, disk I/O, or validation tools.

```bash
python bd2hevc.py start "MY_DISC_BACKUP" "Converted UHD-BD/My Disc (BD) (UHD converted)"
```

Check the newest job:

```bash
python bd2hevc.py status
```

`status` reports duration-weighted encoding progress, so a long movie clip
counts by how far that clip has encoded rather than only as "one clip not
done." Muxing and optional compact audio conversion appear as separate live
lines because they are required follow-up work, but they do not make the top
progress bar fall backward after a large clip finishes encoding.

Watch the whole queue until all currently running or queued jobs finish:

```bash
python bd2hevc.py status --watch
```

Watch a specific job until it finishes:

```bash
python bd2hevc.py status 20260429-153012-MY_DISC_BACKUP --watch
```

In an interactive terminal, `--watch` redraws the same status block in place
instead of appending a new progress bar every refresh. With no number it refreshes
once per second. Add a number to choose another interval, for example
`--watch 10`.

![BD2HEVC status watch screenshot](docs/assets/progress-watch.svg)

List recent jobs:

```bash
python bd2hevc.py jobs
```

Useful job filters:

```bash
python bd2hevc.py jobs --active
python bd2hevc.py jobs --failed
python bd2hevc.py jobs --failed --hide-old-failed
python bd2hevc.py jobs --completed --limit 30
```

Pause the queue after the current running conversion:

```bash
python bd2hevc.py pause-queue
```

Resume queued jobs:

```bash
python bd2hevc.py resume-queue
```

Cancel or remove a queued job:

```bash
python bd2hevc.py cancel 20260429-153012-MY_DISC_BACKUP
python bd2hevc.py remove 20260429-153012-MY_DISC_BACKUP
```

Stopping an already-running conversion is intentionally explicit:

```bash
python bd2hevc.py cancel 20260429-153012-MY_DISC_BACKUP --kill
```

Queue several conversions in one command:

```bash
python bd2hevc.py queue "Disc 1" "Disc 2" "Disc 3" --output-dir "Converted UHD-BD"
```

You can also point `queue` at a parent folder containing multiple BDMV backup
folders:

```bash
python bd2hevc.py queue "BD backups" --output-dir "Converted UHD-BD"
```

Job files, logs, plans, and full reports are written under `reports/jobs/`.
The final converted disc is written to the output folder shown by `start` or
`queue`.

## Quality And Audio Recipes

Use compact CQ for episode-heavy discs or very large movie discs:

```bash
python bd2hevc.py queue "Anime Disc 1" "Anime Disc 2" --output-dir "Converted UHD-BD" --bitrate-mode compact-cq --compact-cq-value 20
```

`compact-cq` keeps the meaning of CQ by using the requested CQ level on each
clip that is long enough to be reencoded.

For storage-limited movie collections, you can spend more bits on the main
feature while keeping extras compact:

```bash
python bd2hevc.py queue "Movie Disc" --output-dir "Converted UHD-BD" --bitrate-mode compact-cq --compact-cq-value 20 --main-title-cq 18
```

`--main-title-cq` applies only to the longest reencoded CQ clip, which is the
usual single-file main movie on many Blu-ray backups. Lower CQ means larger and
higher quality.

For episode discs, use `--top-n-cq COUNT CQ` instead. This applies the chosen CQ
to the longest reencoded CQ clips, which lets a disc with three episodes keep
those episodes at higher quality while extras stay at the general compact CQ:

```bash
python bd2hevc.py queue "Episode Disc" --output-dir "Converted UHD-BD" --bitrate-mode compact-cq --compact-cq-value 20 --top-n-cq 3 18
```

`--top-n-cq` and `--main-title-cq` are mutually exclusive.

If the playback setup is stereo, `--audio-mode compact-stereo` can save a lot of
space on discs with TrueHD/DTS-HD and many dub tracks:

```bash
python bd2hevc.py queue "Movie Disc" --output-dir "Converted UHD-BD" --bitrate-mode compact-cq --compact-cq-value 20 --main-title-cq 18 --audio-mode compact-stereo
```

This converts each audio track in reencoded clips to Blu-ray-friendly AC-3,
using stereo for multi-channel sources and mono for mono sources. PGS subtitles
are still preserved from the source clip. Defaults are `256k` for stereo and
`128k` for mono; adjust them with `--stereo-audio-bitrate` and
`--mono-audio-bitrate`. Audio passthrough remains the default.

## VLC Compatibility Fixes

BD2HEVC defaults to `--vlc-compat auto`: it keeps the source disc structure, then
applies narrow known fixes for VLC/libbluray quirks when the converter can
recognize the affected BD-J bytecode.

For the closest possible copy of the source BD-J, disable those optional fixes:

```bash
python bd2hevc.py auto "Disc" "Converted UHD-BD/Disc (BD) (UHD converted)" --vlc-compat off
```

Apply compatibility fixes to an existing converted output:

```bash
python bd2hevc.py patch-vlc-compat "Converted UHD-BD/Disc (BD) (UHD converted)"
```

The built-in fix can also be selected explicitly:

```bash
python bd2hevc.py auto "Disc" --vlc-fix topmenu-mark-zero-on-return
```

For matching BlueMoon-style BD-J menus, `topmenu-mark-zero-on-return` handles
discs where VLC/libbluray returns to a top-menu playlist at a positive playmark
and the disc app never redraws the menu overlay. It normalizes that top-menu
return to the menu entry point so normal mark events and BD-J graphics updates
fire again.

Advanced users can supply JSON patch files with `--compat-patch-file`. The
current custom format supports JAR entry patching with `replace_hex` and
`replace_method_call` operations, while preserving backups of edited JARs.

## Normal Validation

Validate an output against its source:

```bash
python bd2hevc.py validate "Converted UHD-BD/My Disc (BD) (UHD converted)" --reference "MY_DISC_BACKUP"
```

Skip MakeMKV if it is not installed or you only want FFprobe/decode checks:

```bash
python bd2hevc.py validate "Converted UHD-BD/My Disc (BD) (UHD converted)" --reference "MY_DISC_BACKUP" --no-makemkv
```

Save the full validation report:

```bash
python bd2hevc.py validate "Converted UHD-BD/My Disc (BD) (UHD converted)" --reference "MY_DISC_BACKUP" --report reports/my_disc.validate.json
```

## Automated Support Reports

If a converted backup has a menu, gallery, game, subtitle, audio, or playback
problem that you cannot reproduce on another machine, create a diagnostic bundle:

```bash
python bd2hevc.py diagnose "Converted UHD-BD/My Disc (BD) (UHD converted)" --source "MY_DISC_BACKUP"
```

The command writes a zip under `reports/diagnostics/` and prints the exact path.
Attach that zip to a GitHub issue along with a short description of the playback
steps that fail.

The diagnostic bundle is designed for public bug reports. It includes:

- BD2HEVC version, OS, Python, and discovered media tool versions.
- A file manifest with names, sizes, and timestamps.
- Redacted job metadata, plan, report, exit code, and a log tail when a matching
  background job is found. The default log tail is 5000 lines, and the bundle
  also includes a compact error-highlight file extracted from the full job log.
- A lightweight validation report run without MakeMKV so physical optical drives
  are not touched.

It intentionally does not include `.m2ts` media, BD-J JARs, keys, decryption
logs, or raw disc assets. Local absolute paths are replaced with placeholders.
If BD2HEVC cannot match the output to the correct job automatically, pass the job
id shown by `python bd2hevc.py jobs`:

```bash
python bd2hevc.py diagnose "Converted UHD-BD/My Disc (BD) (UHD converted)" --job 20260429-153012-MY_DISC
```

## Requirements

Supported operating systems:

- Windows
- Linux, including WSL when native Linux media tools are installed

Required:

- Python 3.10+
- FFmpeg and FFprobe with at least one supported HEVC encoder:
  `hevc_nvenc`, `hevc_qsv`, `hevc_amf`, or `libx265`
- tsMuxer 2.7 or newer

Optional but recommended:

- NVIDIA GPU/driver capable of NVENC HEVC for the tested default encoder path
- MakeMKV CLI (`makemkvcon` or `makemkvcon64`) for optional title scanning and
  structural validation
- VLC for headless playback smoke tests

BD2HEVC expects already-decrypted, local `BDMV` folder backups. It does not
bypass copy protection.

By default, full-disc folder conversion does not call MakeMKV. This keeps BD2HEVC
from waking or probing physical optical drives while you are using MakeMKV or
another program to create a backup from a disc. Opt in when you specifically want
that extra title scan:

```bash
python bd2hevc.py auto "Disc" --makemkv
python bd2hevc.py validate "Converted UHD-BD/Disc (BD) (UHD converted)" --makemkv
```

## Installing Tools

Windows:

- FFmpeg can be installed with winget or another package manager.
- MakeMKV is auto-detected from common Windows install folders.
- tsMuxer is auto-detected from the bundled `tools\tsmuxer\` folder, the legacy
  `tools\tsmuxer-2.7.0\` folder, or `PATH`.
- VLC is auto-detected from common Windows install folders.

Linux:

- Put required tools `ffmpeg`, `ffprobe`, and `tsmuxer` or `tsMuxeR` on `PATH`.
  Put optional `makemkvcon` and `vlc` on `PATH` if you want MakeMKV validation
  or VLC smoke tests.
- BD2HEVC intentionally prefers native Linux tools on Linux/WSL and ignores
  Windows `.exe` tools that may appear through WSL interop.
- Make sure your FFmpeg build lists the encoder you plan to use:

```bash
ffmpeg -hide_banner -encoders | grep -E 'hevc_nvenc|hevc_qsv|hevc_amf|libx265'
```

## VLC Java Setup For Menus

BD2HEVC preserves BD-J menus, so VLC needs a Java runtime that libbluray can
load. Without Java, VLC may play the main video but skip or break interactive
menus.

Windows:

- Install 64-bit VLC.
- Install a 64-bit Java runtime. Java 8 is the safest choice for BD-J menus;
  a 64-bit OpenJDK or JDK build is fine.
- Set `JAVA_HOME` to the Java install folder, for example
  `C:\Program Files\Eclipse Adoptium\jdk-8...`.
- Add both `%JAVA_HOME%\bin` and, when present, `%JAVA_HOME%\bin\server` to the
  user or system `PATH`.
- Restart VLC after changing environment variables.

Check Java from a new terminal:

```cmd
java -version
where java
```

Open a backup in VLC:

1. Choose `Media` > `Open Disc`.
2. Select `Blu-ray`.
3. Browse to the backup folder that contains `BDMV`.
4. Leave `No disc menus` unchecked.
5. Press `Play`.

Linux:

- Install VLC, Java, and the BD-J support package for your distribution. On
  Debian/Ubuntu-style systems this is usually:

```bash
sudo apt install vlc openjdk-8-jre libbluray-bdj libbluray-bin
```

- If Java 8 is unavailable, use the distro's default JRE, but BD-J menu
  compatibility can vary by VLC/libbluray build.
- If multiple Java versions are installed, set `JAVA_HOME` before launching VLC:

```bash
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
vlc "bluray:///path/to/backup"
```

Troubleshooting:

- Match VLC and Java architecture on Windows: 64-bit VLC needs 64-bit Java.
- Open `Tools` > `Messages`, set verbosity to `2`, and look for `libbluray`
  messages if menus do not load.
- Some VLC builds are packaged without BD-J support. Try the official VLC build
  on Windows or install your distro's `libbluray-bdj` package on Linux.
- WSL is useful for conversion, but native Windows or native Linux VLC is usually
  the better place to test BD-J menu playback.

References:

- VideoLAN libbluray: <https://images.videolan.org/developers/libbluray.html>
- VLC user documentation: <https://vlc-user-documentation.readthedocs.io/>

Optional editable install:

```bash
python -m pip install -e .
bd2hevc tools
```

## What It Changes

- Copies the full source backup structure.
- Reencodes non-HEVC video clips longer than 10 seconds to HEVC.
- Passes audio and PGS subtitle streams through unchanged by default.
- Optionally converts audio in reencoded clips to compact AC-3 stereo/mono with
  `--audio-mode compact-stereo`.
- Keeps source resolution. No upscaling is done by `auto`.
- Patches CLPI/MPLS video descriptors so replacement clips are described as
  HEVC.
- Adjusts CLPI packet maps so VLC/libbluray does not follow stale packet
  positions from the larger source streams.
- Generates missing disc-library metadata so VLC shows a normal title instead
  of a long `bluray:///...` path.
- Applies known narrow BD-J compatibility fixes where BD2HEVC has an automated,
  disc-specific safe patch and `--vlc-compat` is enabled.

## Bitrate Controls

The default `balanced` mode estimates the HEVC target from the source video-only
bitrate, resolution, frame rate, and source codec. Audio bitrate is ignored so
passthrough audio does not inflate the video target. The AVC/H.264 curve is
anchored around the common HEVC "same quality at roughly half the bitrate"
target, then keeps a safety margin for one-pass hardware encoding and Blu-ray
folder playback.

MPEG-2 sources get codec-aware bitrate handling. When FFprobe reports a Blu-ray
MPEG-2 CPB ceiling instead of the actual clip bitrate, BD2HEVC falls back to the
container bitrate minus known audio. The 10-second rule still applies: MPEG-2
clips at or below 10 seconds are copied.

Presets:

```bash
python bd2hevc.py auto "Disc" --bitrate-mode smaller
python bd2hevc.py auto "Disc" --bitrate-mode balanced
python bd2hevc.py auto "Disc" --bitrate-mode transparent
python bd2hevc.py auto "Disc" --bitrate-mode source-ratio
python bd2hevc.py auto "Disc" --bitrate-mode compact-cq
```

`compact-cq` is intended for space-focused conversions where CQ 18 is preferred
over the source-equivalent bitrate curve. It is useful for multi-episode/anime
discs and can also make high-bitrate movie discs substantially smaller than the
balanced preset.
By default, every clip above BD2HEVC's 10-second reencode threshold uses HEVC
CQ 18. With `hevc_nvenc`, CQ clips use a lean HandBrake-like CQ command path
instead of BD2HEVC's normal AQ/VBV-heavy movie tuning. It also avoids FFmpeg's
`-bluray-compat` shortcut for those CQ clips because that option can raise
bitrates at the same CQ value; BD2HEVC still keeps explicit AUD/GOP/metadata
controls for authored disc playback. `compact-cq` currently supports
`hevc_nvenc`, `hevc_amf`, and `libx265`; use `balanced`, `smaller`, or another
bitrate mode with `hevc_qsv`. The CQ cutoff can be raised if you only want
episode/movie-length clips to use CQ:

```bash
python bd2hevc.py auto "Disc" --bitrate-mode compact-cq --compact-cq-min-duration 20m
```

The CQ value can be adjusted too. Higher CQ values are smaller/lower quality;
lower CQ values are larger/higher quality:

```bash
python bd2hevc.py auto "Disc" --bitrate-mode compact-cq --compact-cq-value 20
```

For anime encodes similar to HandBrake's H.265 10-bit option, add:

```bash
python bd2hevc.py auto "Disc" --bitrate-mode compact-cq --hevc-bit-depth 10
```

Custom presets can be put in a JSON file so repeat conversions do not need a
long command line:

```json
{
  "mode": "compact-cq",
  "compact_cq_value": 20,
  "compact_cq_min_duration": "10s",
  "max_video_bitrate": "70M"
}
```

Use it like this:

```bash
python bd2hevc.py auto "Disc" --bitrate-preset-file examples/bitrate/compact-cq20.json
```

Preset files can set `mode`, `hevc_bitrate_factor`, `min_video_bitrate`,
`max_video_bitrate`, `maxrate_multiplier`, `bufsize_multiplier`,
`compact_cq_value`, and `compact_cq_min_duration`. Non-default CLI flags still
work for one-off overrides.

Manual controls:

```bash
python bd2hevc.py auto "Disc" --hevc-bitrate-factor 0.62
python bd2hevc.py auto "Disc" --min-video-bitrate 2500k --max-video-bitrate 60M
python bd2hevc.py auto "Disc" --maxrate-multiplier 1.5 --bufsize-multiplier 2.0
```

## Supported So Far

These are observations from local testing, not a formal compatibility guarantee.
At the time of writing, every locally converted backup available for spot checks
has worked in VLC/libbluray, but future discs may still expose new BD-J or player
edge cases. The list below describes coverage shape, not a promise that every
edition or region behaves identically.

| Coverage area | Locally tried examples | Notes |
| --- | --- | --- |
| Movie discs | Dune, Dune Part Two, Groundhog Day, The Princess Bride, Ferris Bueller's Day Off, Interstellar, Tenet, The Truman Show, Walter Mitty, Goodbye Mr. Chips | Main playback, menu return, and extras workflows have been spot checked in VLC. |
| Bonus and non-feature discs | Interstellar bonus disc, Back to the Future bonus disc | Handles discs without an obvious single main title. |
| Episode and compact CQ discs | One Punch Man, Baccano!, Tensura/Re:Zero-style episode discs, BBC Pride and Prejudice | Covers multi-episode layouts, `compact-cq`, and `--top-n-cq` workflows. |
| Interactive BD-J extras | Speed, The Truman Show galleries, game-containing discs | Covers BD-J games, galleries, and menu timing repairs after CLPI/navigation fixes. |
| Optional validation helpers | MakeMKV title scanning, VLC smoke logs, diagnostic bundles | Useful for catching structure/player issues without sharing disc assets. |

Well-supported in current testing:

- Full-disc BD-J menu backups with the original menus and extras preserved.
- AVC/H.264 video reencoded to 8-bit HEVC Main with audio/subtitles passed
  through.
- MPEG-2 menu/gallery clips selected by the 10-second rule, including sparse
  still-like clips.
- VLC/libbluray folder playback on Windows with D3D11 hardware decoding.
- Discs with BD-J games or interactive extras, based on successful testing with
  a game-containing disc after the CLPI/menu timing fixes.
- MakeMKV title scanning as an optional structural validation layer.

Known limits and watch areas:

- BD2HEVC is not certified UHD-BD authoring software. The output is aimed at
  local folder playback in VLC/libbluray-style players.
- Every BD-J disc can do unusual things. If a menu, gallery, or game behaves
  oddly, keep the source and output and run validation/probes before deleting
  anything.
- `topmenu-mark-zero-on-return` was validated against a disc with a VLC-only
  top-menu redraw failure and is applied only when the matching BD-J wrapper
  signature is detected.
- VLC can log `buffer deadlock prevented`, `blurayReleaseVout`, or
  `SetThumbNailClip failed` during BD-J menu/gallery switching even when the
  output remains usable. D3D11 allocation failures are more suspicious and
  should be reported with logs.
- Main10 output is available, but the tested VLC path has been more reliable
  with the default 8-bit Main output for 8-bit BD sources.

## Repair And Diagnostics

Repair an older output with current converter rules:

```bash
python bd2hevc.py repair-output "MY_DISC_BACKUP" "Converted UHD-BD/My Disc (BD) (UHD converted)"
```

Preview the repair plan:

```bash
python bd2hevc.py repair-output "MY_DISC_BACKUP" "Converted UHD-BD/My Disc (BD) (UHD converted)" --dry-run
```

Run a headless VLC/libbluray smoke test without opening a visible video window:

```bash
python bd2hevc.py vlc-smoke "Converted UHD-BD/My Disc (BD) (UHD converted)"
```

Test the Windows D3D11 path:

```bash
python bd2hevc.py vlc-smoke "Converted UHD-BD/My Disc (BD) (UHD converted)" --video-plane --d3d11
```

Probe a specific playlist through libbluray/FFprobe:

```bash
python bd2hevc.py playlist-probe "Converted UHD-BD/My Disc (BD) (UHD converted)" --playlist 23 --reference "MY_DISC_BACKUP" --decode-seconds 24
```

Patch missing disc metadata on existing outputs:

```bash
python bd2hevc.py patch-disc-metadata "Converted UHD-BD"
```

Create a redacted bundle for a GitHub support issue:

```bash
python bd2hevc.py diagnose "Converted UHD-BD/My Disc (BD) (UHD converted)" --source "MY_DISC_BACKUP"
```

## JSON Output

Normal commands print human summaries. Use these when you want detailed reports:

```bash
python bd2hevc.py auto "Disc" --report reports/disc.convert.json
python bd2hevc.py auto "Disc" --json
python bd2hevc.py validate "Output" --reference "Disc" --report reports/disc.validate.json
python bd2hevc.py tools --json
```

## Legal

Use BD2HEVC only with backups you are legally allowed to process. The project
does not include or provide decryption, keys, disc data, BD-J source code, or
copyrighted assets.
