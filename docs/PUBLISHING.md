# Publishing BD2HEVC

## GitHub Setup

The public repository currently lives at:

<https://github.com/nathanturk2/BD2HEVC>

For a new mirror or fork, start from the clean release folder:

```bash
git init
git add .
git commit -m "Initial BD2HEVC release"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/bd2hevc.git
git push -u origin main
```

Then on GitHub:

1. Create a public repository named `bd2hevc` or `BD2HEVC`.
2. Add the topics `blu-ray`, `bdmv`, `hevc`, `h265`, `ffmpeg`, `tsmuxer`,
   `vlc`, `libbluray`, `bd-j`, `nvenc`, `makemkv`, `python`, and
   `physical-media`.
3. Enable Issues and Discussions if you want users to report disc compatibility
   results. The repository includes issue forms and a suggested label set in
   `.github/labels.yml`.
4. Confirm the bundled CI workflow passes on GitHub Actions.
5. Add an alpha release tag when ready:

```bash
git tag -a v0.1.0-alpha -m "BD2HEVC v0.1.0-alpha"
git push origin v0.1.0-alpha
```

## Release Readiness Note

`v0.1.0-alpha` should be presented as an alpha release, but the code structure
is now reasonable for public development. The major low-level concerns are
separated into modules: BD-J patching, bitrate planning, encoding, muxing,
navigation metadata, output repair, progress rendering, queueing, scanning,
tool discovery, validation, and output handling. The remaining `core` module is
primarily orchestration and CLI glue.

Do not overpromise disc compatibility. The strongest claim is that the tested
workflow preserves full-disc menus/extras and has worked across the local test
set so far, with modular hooks for future VLC/libbluray compatibility fixes.

## Suggested GitHub Description

Full-disc Blu-ray folder backup reencoder: converts video to HEVC/H.265 while
preserving menus, extras, audio, subtitles, playlists, BD-J, and VLC/libbluray
folder playback.

## Suggested First Post

Use `docs/REDDIT_ANNOUNCEMENT.md` as the current draft. Keep the first public
post narrow and careful:

- It works on local, unencrypted BDMV folder backups.
- It does not decrypt discs, provide keys, or include copyrighted assets.
- It is for reducing storage while preserving the full-disc menu experience.
- Ask testers to include OS, GPU, FFmpeg version, tsMuxeR version, command used,
  and validation output when filing issues.

## Possible Reddit Communities

Read each community's current rules before posting. Avoid anything that sounds
like a piracy or decryption request.

- `r/DataHoarder`: likely interested in storage reduction, preservation
  tradeoffs, and long-running conversion workflows.
- `r/ffmpeg`: good for encoder/rate-control feedback, but keep posts technical
  and focused on FFmpeg command construction.
- `r/Bluray`: possibly interested in disc-menu preservation, though rules and
  tolerance for backup-tool posts may vary.
- `r/homelab` or `r/selfhosted`: useful only if framing it as a batch archival
  workflow for personal media libraries.

For Reddit, lead with the technical angle: HEVC reencoding while preserving
menus/extras and VLC playback. Do not lead with ripping, decryption, or any
specific commercial disc.
