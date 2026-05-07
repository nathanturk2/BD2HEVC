# Contributing

Thanks for helping improve BD2HEVC.

## Scope

BD2HEVC works with local, unencrypted Blu-ray folder backups. Please do not
contribute code, documentation, samples, test assets, or instructions that
decrypt discs, bypass copy protection, include keys, or distribute copyrighted
disc data.

## Useful Bug Reports

Please include:

- Operating system and version.
- Python version.
- FFmpeg/FFprobe version.
- tsMuxeR version.
- GPU and selected encoder, for example `hevc_nvenc`.
- Exact command used.
- Whether VLC playback, validation, menus, extras, galleries, or games failed.
- The relevant `reports/jobs/*.log` and `*.report.json` files with personal
  paths redacted if desired.

## Local Checks

Run these before sending a patch:

```bash
python -m py_compile bd_to_uhdbd.py bd2hevc.py
python -m unittest discover -s tests
python bd2hevc.py --help
python bd2hevc.py tools
```

For conversion changes, also run at least one `--dry-run` and one real
conversion on a short backup you own.

## Style

- Prefer small, behavior-focused patches.
- Keep the default workflow faithful: preserve menus/extras/audio/subtitles and
  do not upscale unless the user explicitly asks for it.
- Keep MakeMKV optional for full-disc conversion so the tool does not probe
  optical drives during normal folder-backup work.
