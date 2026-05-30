# Reddit Announcement Draft

Recommended first subreddit: `r/makemkv`

## Title

BD2HEVC: shrink full-disc Blu-ray backups to HEVC while keeping menus intact

## Body

Hi all. I am the maintainer of BD2HEVC, a GPL-3.0 open-source tool I built for
my own physical media backup workflow.

The niche it tries to solve is simple: I wanted smaller Blu-ray backups, but I
did not want to lose the disc experience. MKV workflows are great, but they do
not preserve the original menus, extras navigation, BD-J behavior, galleries,
games, and other authored-disc features.

BD2HEVC works on an existing local BDMV backup and:

- reencodes video clips to HEVC/H.265
- preserves the original menu structure, extras, playlists, BD-J files,
  subtitles, chapters, navigation metadata, and audio by default
- optionally converts audio to compact stereo/mono AC-3 for storage-limited
  setups
- uses NVENC by default, with QSV, AMF, and libx265 selectable when supported by
  your FFmpeg build
- has queue/status commands for background conversions
- has optional VLC/libbluray compatibility patches
- runs on Windows and Linux
- keeps the original resolution; no upscaling in the normal workflow

It does not download media, provide keys, decrypt discs, or bypass copy
protection. It expects a BDMV folder backup you already have.

GitHub:
https://github.com/nathanturk2/BD2HEVC

So far I have tried dozens of disc backups across movies, TV/anime discs, bonus
discs, galleries, and BD-J interactive content, and the converted outputs
available for spot checks currently work in VLC/libbluray. That said, Blu-ray
menus are weird, so I would especially appreciate feedback from people with
unusual BD-J discs, menu-heavy releases, TV/anime discs, or bonus-disc workflows.

If something breaks, BD2HEVC has a `diagnose` command that creates a redacted
support bundle without including media files, keys, or BD-J JARs. This started
as a personal tool, so I am sure there are workflows I have not thought of yet.

## Posting Notes

- Lead with menu preservation and storage reduction, not ripping or decryption.
- Disclose that you are the maintainer.
- Say alpha/community-tested rather than universal compatibility.
- Link the GitHub repo once.
- Ask for diagnostic bundles for broken menus, galleries, games, subtitles,
  audio, Linux, and non-NVIDIA encoder cases.
