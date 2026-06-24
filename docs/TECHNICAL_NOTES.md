# Technical Notes

BD2HEVC is built around a conservative full-disc workflow:

1. Copy the original backup structure.
2. Inspect every clip in `BDMV/STREAM`.
3. Reencode video clips longer than 10 seconds to HEVC. The tested default
   encoder is NVENC, with QSV, AMF, and `libx265` also supported when FFmpeg
   provides them.
4. Pass audio and PGS subtitle streams through unchanged.
5. Remux replacement clips with tsMuxer.
6. Patch the Blu-ray navigation metadata that describes the primary video stream.
7. Validate the output with FFprobe/decode samples and, when enabled, MakeMKV.
8. Generate fallback disc-library metadata when the source backup is missing it.

The tool intentionally keeps source resolution. The default `auto` workflow does
not upscale 1080p Blu-ray video to 2160p.

## User-Facing Commands

The intended normal commands are:

- `tools`: readable dependency check.
- `auto`: foreground full-disc conversion with live progress and a short final
  summary.
- `start`: create a plan and launch the same full-disc conversion in the
  background queue.
- `status`: read the saved plan/log/exit-code files for a background job and
  show progress.
- `jobs`: list recent background conversions.
- `queue`: enqueue several source backups in one command.
- `validate`: concise output validation summary, with `--report` or `--json`
  for full details.

The older lower-level commands remain available for repair and diagnostics, but
normal users should not need to manually combine dry-run JSON, redirected logs,
and the raw `progress` command.

Background workers enforce a simple FIFO queue by job creation order. Multiple
`start` or `queue` jobs may have waiting worker processes, but only the oldest
unfinished job is allowed to run the actual conversion command.

## VLC Compatibility Patch Model

The default full-disc commands use `--vlc-compat auto`. This keeps the original
disc files as the baseline, then applies only recognized VLC/libbluray
compatibility fixes. `--vlc-compat off` disables those optional BD-J/JAR edits
for a more literal source copy.

Built-in fixes are named and can be selected with repeated `--vlc-fix` flags.
Custom JSON patch files can be supplied with `--compat-patch-file`. The custom
format is intentionally small: each patch targets a JAR glob and entry, then
runs `replace_hex` or `replace_method_call` operations. Signature files in
`META-INF` are removed by default when a JAR is edited, and the original JAR is
backed up before replacement.

Example custom patch file:

```json
{
  "patches": [
    {
      "id": "example-repaint-call",
      "jar_glob": "*.jar",
      "entry": "Example.class",
      "remove_signatures": true,
      "operations": [
        {
          "type": "replace_method_call",
          "label": "call repaint instead of requestFocus",
          "opcode": "invokevirtual",
          "from_class": "java/awt/Component",
          "from_name": "requestFocus",
          "from_descriptor": "()V",
          "to_class": "java/awt/Component",
          "to_name": "repaint",
          "to_descriptor": "()V",
          "expected_matches": 1
        }
      ]
    }
  ]
}
```

The current built-in VLC fixes are `music-jukebox-queued-state` and
`topmenu-mark-zero-on-return`. Auto detection applies each fix only when the
matching BD-J bytecode signature is present.

`music-jukebox-queued-state` handles a Warner-style music jukebox helper where
the button starts the music-jukebox state before queuing the matching jukebox
menu buttons. The compatibility patch keeps the same helper method and state
name, but first queues the disc's own previous-menu close/show calls, queues
the authored jukebox popup and track group together, and only then queues the
state change so VLC/libbluray has the track-picker menu in place before playlist
selection. When extracted Warner menu resource folders are present, the patch
also appends the existing playlist radio group to the existing jukebox popup's
children list; it does not generate art, add z-index overrides, or create a new
background layer. The state handler is also guarded so if VLC/libbluray reaches
the music-jukebox state with no current button, the disc's authored default
track button is restored before the original playback-helper logic runs.

`topmenu-mark-zero-on-return` was developed against a disc with a VLC-only
top-menu redraw failure, but auto detection applies it to other discs only when
the same BlueMoon-style playlist wrapper signature is present.

The top-menu fix normalizes playlist returns from a positive playmark to the
menu entry point. This avoids a VLC/libbluray path where the top-menu title
starts but no mark events or BD-J graphics updates are emitted, leaving only the
menu backdrop visible.

Earlier HScene repaint/show experiments were kept out of the public
compatibility set because they did not resolve the validated failure mode. The
project should prefer one proven narrow patch over a pile of hopeful menu
mutations.

## Video Rules

The full-disc converter reencodes any clip with video duration greater than 10
seconds. Shorter clips are copied so tiny menu assets and still-like assets are
less likely to be disturbed.

Some Blu-ray menu/gallery clips are sparse: a handful of frames are timestamped
across a much longer duration. When these clips are longer than 10 seconds,
BD2HEVC expands them to constant-frame-rate HEVC with cloned frames so the
output duration stays aligned with the source instead of collapsing to the
decoded frame count.

Sparse replacements are encoded without B-frames and with no NVENC lookahead.
These clips are usually menu/gallery stills, so B-frames add little value while
making VLC's D3D11 hardware-decoder path work harder during rapid BD-J stream
switches.

The default HEVC output is 8-bit Main profile because the tested BD sources were
8-bit and VLC's D3D11 path handled 8-bit menu clips more reliably than Main10
replacement clips.

## Bitrate Model

BD2HEVC estimates the target from video-only source bitrate. Audio is ignored so
large passthrough audio tracks do not inflate the HEVC target.

Full-disc planning is metadata-first. The initial FFprobe pass establishes
duration, codec, dimensions, tracks, and candidate actions; quality and clip
overrides are then applied to a preview plan. Packet-size summation and coded
padding analysis run only for final reencode actions using VBR/source-ratio
targets. Fixed CQ does not consume source bitrate when constructing the encoder
command, so omitting those full-file reads changes planning time rather than
converted output.

The default AVC/H.264-to-HEVC curve is intentionally an estimate, not a promise:
there is no single bitrate that is mathematically correct from source bitrate
alone. The model is anchored to HEVC's common target of comparable perceptual
quality at about half the H.264/AVC bitrate, while leaving extra room for
one-pass hardware encoding and authored-disc compatibility constraints. The
current `balanced` AVC curve lands around 0.48-0.55 of the source video bitrate
depending on source bits-per-pixel-per-frame; `transparent` keeps the older,
more conservative margin by scaling that curve upward.

Useful references:

- IEEE CASS summary of Sullivan, Ohm, Han, and Wiegand's HEVC overview:
  <https://ieee-cas.org/media/overview-high-efficiency-video-coding-hevc-standard>
- JCT-VC subjective verification-test summary:
  <https://www.microsoft.com/en-us/research/publication/hevc-subjective-video-quality-test-results/>
- NVIDIA NVENC rate-control documentation:
  <https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html>
- x265 CRF/rate-control documentation:
  <https://x265.readthedocs.io/en/stable/cli.html>

MPEG-2 sources are treated as less compression-efficient than AVC. When FFprobe
reports the Blu-ray MPEG-2 CPB ceiling instead of the actual clip bitrate,
BD2HEVC falls back to the container bitrate minus known audio. That keeps
low-bitrate menu/gallery stills from being encoded at a bogus 40 Mbps-derived
target when they are long enough to be selected for HEVC replacement.

The presets are:

- `smaller`: more aggressive space saving.
- `balanced`: default tested setting, centered on a source-equivalent HEVC
  estimate with a modest hardware-encoding margin.
- `transparent`: higher target for harder material.
- `source-ratio`: fixed source video bitrate ratio.
- `compact-cq`: uses CQ 18 by default for every clip above the 10-second
  reencode threshold. This is aimed at compact storage for multi-episode discs
  and high-bitrate movie discs where CQ is preferred over the source-equivalent
  bitrate curve. On `hevc_nvenc`, CQ clips
  intentionally use a HandBrake-like CQ path without the normal
  spatial/temporal AQ and VBV tuning or FFmpeg's `-bluray-compat` shortcut
  because those options can materially increase bitrate at the same CQ value.
  Explicit AUD/GOP/metadata controls are still used for authored disc playback.

Advanced users can override the curve with `--hevc-bitrate-factor`, plus
`--min-video-bitrate`, `--max-video-bitrate`, `--maxrate-multiplier`, and
`--bufsize-multiplier`. For reusable presets, `--bitrate-preset-file` accepts a
JSON object with `mode`, `hevc_bitrate_factor`, `min_video_bitrate`,
`max_video_bitrate`, `maxrate_multiplier`, `bufsize_multiplier`,
`compact_cq_value`, and `compact_cq_min_duration`. Non-default CLI flags
override matching preset fields.

## BD-J And Navigation

BD2HEVC does not try to replace menu logic. It preserves the original `BDJO`,
`JAR`, playlist, clip-info, subtitle, audio, and auxiliary files. Known
compatibility patches are applied narrowly and only where the automated
converter knows how to make a safe disc-specific adjustment.

For replacement clips, CLPI/MPLS primary video descriptors are patched from the
source video codec to HEVC while timing and playlist structure stay aligned with
the original backup. BD2HEVC restores the source CLPI, patches the video
descriptor, then scales the existing CPI packet map to the authored HEVC M2TS
packet count. This keeps BD-J-facing CLPI structure close to the source while
preventing VLC/libbluray from following stale AVC packet positions past the end
of the smaller HEVC stream.

Compact-audio remuxes also update navigation metadata. CLPI program-info and
MPLS stream-coding descriptors are changed from LPCM/other source audio types to
AC-3, and the existing CPI packet map is scaled from the pre-remux M2TS packet
count to the compact output. For in-place repair, the previous M2TS and CLPI
bytes remain available until stream count, codecs, channels, video codec,
subtitle codecs, timestamps, duration, and decode checks pass. Its navigation
descriptors are patched before the next clip starts. A failed media validation
is rolled back without requiring a second full-disc backup.

If a source backup has no usable `BDMV/META/DL/bdmt_*.xml`, BD2HEVC creates a
minimal `bdmt_eng.xml` with a cleaned disc title. This gives VLC/libbluray a
normal disc name and avoids falling back to a long `bluray:///...` folder path
for title display.

## Validation

Validation checks include:

- Long video clips are HEVC.
- Audio codec lists match the source.
- Compact audio has the expected playable stream count, AC-3 codec, mono/stereo
  channel layout, and configured bitrate.
- Audio-only remuxes preserve the video codec and subtitle codec list.
- Source and output clip start timestamps stay aligned.
- Long replacement clip durations stay aligned with the source.
- Sparse source clips keep their source duration after HEVC replacement.
- Sparse HEVC replacement clips do not contain B-frames.
- Optional decode samples can be run on replacement clips.
- `playlist-probe` can validate a whole Blu-ray playlist through libbluray and
  fail on missing duration, missing video, `Read past EOF`, low frame counts, or
  source/output duration drift.
- Optional MakeMKV title scans can catch structural problems that probe/decode
  checks may miss.

MakeMKV is optional by default so Linux and Windows users can still convert when
it is unavailable. Use `--require-makemkv` when you want title-scan failures to
make the command fail.

## Linux Notes

The code discovers `ffmpeg`, `ffprobe`, `tsmuxer`/`tsMuxeR`, `makemkvcon`, and
`vlc` from `PATH`. On Linux and WSL it prefers native POSIX tools and ignores
Windows `.exe` tools that may appear through mounted Windows paths. Linux
support expects an FFmpeg build with at least one supported HEVC encoder
(`hevc_nvenc`, `hevc_qsv`, `hevc_amf`, or `libx265`) and tsMuxer available as an
executable. The default encoder path still expects `hevc_nvenc` and a working
NVIDIA driver stack unless you pass another `--encoder`.

Run:

```bash
python bd2hevc.py tools
```

before a large conversion.
