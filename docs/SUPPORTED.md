# Supported And Known Limits

This is based on local testing so far, not a certification promise.

## Works Well So Far

- Local, unencrypted BDMV folder backups.
- Full-disc BD-J menu preservation.
- Extras, galleries, and BD-J interactive content.
- Audio and subtitle passthrough.
- AVC/H.264 video to HEVC/H.265.
- MPEG-2 sources with codec-aware bitrate planning.
- Compact CQ conversions, including anime TV sets and high-bitrate movie discs,
  with `--bitrate-mode compact-cq`.
- VLC/libbluray folder playback on Windows.
- Background queue workflows with live status watching.

## Optional Helpers

- VLC smoke tests can catch some player-startup issues without opening a visible
  playback window.
- MakeMKV CLI title scanning can be enabled with `--makemkv` or required with
  `--require-makemkv`.

## Known Limits

- BD2HEVC is not certified UHD-BD authoring software.
- It does not decrypt discs and does not provide keys.
- The primary target is local folder playback in VLC/libbluray-style players.
- Disc-specific BD-J logic can be unusual; some discs may require new modular
  VLC compatibility patches.
- Hardware encoding quality and supported options depend on your FFmpeg build
  and GPU driver.
- The default workflow preserves source resolution; no upscaling is performed
  unless an explicit mode/flag does so.
