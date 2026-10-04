# BD2HEVC 0.2.0a2 alpha

The update brings the full Windows GUI to the previously announced
command-line project: conversion planning, clip/playlist controls, presets,
folder or ISO output, batch queues, watched folders, progress and diagnostics.
The normal workflow preserves resolution and colour information while
reencoding video to HEVC. Menus, extras, playlist/branch structure, chapters,
audio and subtitles remain in the backup.

This version also preserves tsMuxeR's secondary-audio role metadata for
passthrough tracks, records the source and authored audio identities for
validation, and retains failed forced-replacement diagnostics outside the
discarded staging area. Existing output replacement still requires successful
validation. DVD and Blu-ray naming helpers remain independently packaged.

## Local validation, 5 October 2026

- All 185 Python tests pass, including mixed audio codec/role authoring and
  failed-replacement diagnostic retention.
- A fresh generated 16-second 720p24 AVC/AC-3 Colour Lab backup passed software
  x265 conversion, decode checks, ISO/UDF descriptors and file payload hashes.
  Source 11,104,248 bytes; final ISO 8,404,992 bytes; conversion 11.75 seconds.
  See [the generated receipt](demo-result.json).
- The generated final ISO decoded video/audio in stock VLC and passed selected
  native pause/resume and seek controls. This fixture has no commercial menus.
- Release artifact checks verify every exported source hash, run exported
  tests and tool provenance, compile the Windows launcher, and install the
  wheel outside the checkout to verify GUI/resources/entry points.

The documented collection contains 117 converted Blu-ray examples in its
30 September catalogue. That is inventory/conversion evidence, not fresh
exhaustive playback of every film or menu. Keep original backups and test
important routes. NVENC has the strongest commercial evidence; QSV/AMF need
their own hardware release matrix. Windows is the desktop target. HEVC output
does not imply 4K/HDR conversion or physical UHD player certification.

GPL-3.0 source and the Hadris MIT fork source/binary provenance are included.
External tools and disc assets are excluded. See [PUBLISHING.md](PUBLISHING.md)
for publication steps. Source and matching alpha downloads are published through
[BD2HEVC on GitHub](https://github.com/nathanturk2/BD2HEVC). The validation above
is local; hosted CI has its own separately recorded results.
