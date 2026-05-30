# Supported And Known Limits

This is based on local testing so far, not a certification promise.

At the time of writing, every locally converted backup available for spot
checking has worked in VLC/libbluray. That is useful evidence, but BD-J discs can
still do unusual things and future discs may expose new compatibility fixes.

## Local Coverage Examples

| Coverage area | Locally tried examples | Notes |
| --- | --- | --- |
| Movie discs | Dune, Dune Part Two, Groundhog Day, The Princess Bride, Ferris Bueller's Day Off, Interstellar, Tenet, The Truman Show, Walter Mitty, Goodbye Mr. Chips | Main playback, menu return, and extras workflows have been spot checked in VLC. |
| Bonus and non-feature discs | Interstellar bonus disc, Back to the Future bonus disc | Handles discs without an obvious single main title. |
| Episode and compact CQ discs | One Punch Man, Baccano!, Tensura/Re:Zero-style episode discs, BBC Pride and Prejudice | Covers multi-episode layouts, `compact-cq`, and `--top-n-cq` workflows. |
| Interactive BD-J extras | Speed, The Truman Show galleries, game-containing discs | Covers BD-J games, galleries, and menu timing repairs after CLPI/navigation fixes. |
| Optional validation helpers | MakeMKV title scanning, VLC smoke logs, diagnostic bundles | Useful for catching structure/player issues without sharing disc assets. |

## Works Well So Far

- Windows and Linux conversion workflows.
- WSL conversion workflows when native Linux tools are installed on the WSL
  `PATH`.
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
