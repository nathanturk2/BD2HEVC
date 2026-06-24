# Architecture

BD2HEVC is intentionally conservative: it treats the source backup as the
authority for menu logic, playlists, audio, subtitles, and disc metadata. The
converter changes video streams and the minimum navigation metadata needed for
players to consume those replacement streams.

## Module Layout

- `bd2hevc.py` is the user-facing script launcher.
- `bd_to_uhdbd.py` is a compatibility wrapper for older scripts/imports.
- `bd2hevc_app.cli` owns the installed console entry point.
- `bd2hevc_app.__main__` supports `python -m bd2hevc_app`.
- `bd2hevc_app.config` owns project constants, tool search paths, presets, and
  compatibility-fix names.
- `bd2hevc_app.bdj` owns BD-J JAR/class bytecode compatibility patching,
  built-in VLC/libbluray patch detection, and custom compatibility patch files.
- `bd2hevc_app.muxing` owns tsMuxeR track parsing, meta-file generation, and
  Blu-ray/M2TS authoring wrappers.
- `bd2hevc_app.navigation` owns CLPI/MPLS descriptor patching, source CLPI
  restoration, CPI packet-map scaling, and optional CPI refresh generation.
- `bd2hevc_app.encoding` owns FFmpeg HEVC command construction and encoder
  option selection.
- `bd2hevc_app.repair` owns converted-output repair selection plus clip remux
  and reencode repair helpers.
- `bd2hevc_app.tools` owns external tool discovery and subprocess execution.
- `bd2hevc_app.bitrate` owns duration parsing, bitrate presets, CQ handling, and
  source-codec-aware HEVC target calculation.
- `bd2hevc_app.scan` owns BDMV discovery, MakeMKV robot parsing, FFprobe stream
  inspection, per-clip action planning, and title/clip summaries.
- `bd2hevc_app.validation` owns clip validation, MakeMKV title validation, and
  playlist probing checks.
- `bd2hevc_app.output` owns output path safety, preservation copy behavior,
  retrying locked-file replacement, fallback disc metadata, and short human
  summaries.
- `bd2hevc_app.progress` owns progress event writing, log/status parsing, and
  in-place watch rendering.
- `bd2hevc_app.queueing` owns background job records, queue pause/resume/cancel
  behavior, status output, and hidden background process launching.
- `bd2hevc_app.core` owns the remaining disc conversion orchestration, command
  wrappers, enqueue planning, and parser dispatch.

The split keeps old command wrappers stable while moving low-level helpers into
focused modules. The remaining `core` code is mostly high-level orchestration
and CLI glue, so further splitting is optional rather than necessary for an
initial open-source release.

## Pipeline

1. Discover tools: FFmpeg/FFprobe and tsMuxeR are required; VLC and MakeMKV are
   optional.
2. Scan the BDMV `STREAM` folder with FFprobe.
3. Plan clips:
   - video clips over 10 seconds are reencoded to HEVC unless already HEVC;
   - shorter clips are copied;
   - audio and subtitles pass through by default;
   - fixed-CQ plans skip packet-accurate bitrate work that cannot affect their
     encoder settings.
4. Copy the disc tree while skipping stream files that will be replaced.
5. Encode selected video streams to temporary raw HEVC.
6. Remux each replacement stream with the original clip's audio/subtitles using
   tsMuxeR.
7. Restore and patch CLPI/MPLS metadata so VLC/libbluray sees HEVC replacement
   clips with sane stream descriptors, packet maps, and timing.
8. Apply optional VLC/libbluray compatibility patches.
9. Validate selected output clips and optionally scan titles with MakeMKV.

With `--audio-mode compact-stereo`, playable audio is converted for both HEVC
replacement clips and copied/already-HEVC clips. The latter use an audio-only
remux: video and subtitles are stream-copied, CLPI/MPLS audio descriptors are
patched to AC-3, and CPI packet positions are rescaled.

`repair-compact-audio` applies that audio-only path to an existing converted
backup without a source backup. It keeps the original M2TS and navigation bytes
until the replacement passes media validation, then patches that clip's
navigation before advancing. This allows per-clip rollback without a second
full-disc copy and avoids deferring all navigation work to the end of a long
repair.

## Queue Model

The background queue runs one disc conversion at a time. Within a conversion,
hardware HEVC encoders can use a bounded encode-to-mux queue:

- one encoder produces HEVC temp clips;
- one muxer consumes them serially;
- `--encode-ahead-depth` caps completed temp clips waiting for muxing.

CPU `libx265` stays serial to avoid oversubscribing the machine.

## Compatibility Philosophy

The default full-disc mode does not upscale and does not replace menus. Optional
compatibility fixes should be modular, narrowly targeted, and documented. A
fix should ideally improve VLC/libbluray playback without changing the user's
visible disc flow.
