# Building and publishing the alpha releases

The versions are **DVD2HEVC 0.2.0a1** (new default UHD-BD output) and
**BD2HEVC 0.2.0a2** (the GUI update). Both projects remain independently
installable. Preparation does not post an announcement, create a GitHub release
or publish a package.

## Review and build

1. Review the code, documentation, notices and intended public file list.
   Keep commercial media, captures, work/logs, private reports and external
   player/tool binaries out. The Windows Hadris author is the deliberate
   exception: complete fork source, MIT notice and provenance are included.
2. Use a deliberate public Git author identity and commit the intended source.
   `python tools/build-release.py --require-clean` exports exact committed
   bytes into a positive source manifest and builds the source ZIP, wheel and
   sdist from that export. A build without this flag explicitly records that
   it includes working-tree changes; do not confuse it with a clean release.
3. `python tools/check-artifacts.py` checks every exported hash/size, runs the
   exported-source tests, compiles the Windows launcher, and installs the wheel
   in a fresh environment outside the checkout. It verifies installed GUI,
   console/module entry points, resources and per-user state.
4. Run `tools/check-shared.py` and `tools/check-provenance.py`. DVD includes its
   native inspector pins, Java source and the Hadris fork inventory; BD includes
   its Hadris source/binary inventory. Build the bundled/native components from
   their documented pinned inputs when changing them.
5. Run `tools/make-demo.py NEW_DIRECTORY --convert` with real tools. DVD's new
   default uses stock VLC with BD-J and Java, tsMuxeR and Hadris. The private VLC
   build workflow is only required when testing/distributing the legacy HEVC DVD
   player. Preserve the receipt and the exact versions/encoder used.
6. Exercise representative original menus, extras, subtitles, audio, chapters,
   seeking/resume and shared/branching routes. UHD-BD's 13-disc author corpus
   is documented separately from the catalogue's 117 legacy converted DVDs.
   BD's approved catalogue contains 117 converted Blu-ray examples. Neither
   catalogue claims fresh exhaustive full-film playback certification.

DVD's public UHD-BD limits include ambiguous audio boundaries, whole-title
decoded continuity, some native chapter/UOP behavior, angle/random playback
and broader timed visual comparisons. State these clearly. NVENC commercial
encoding evidence differs from the generated x265 integration run; QSV/AMF
need their own hardware matrix. Hosted CI is prepared but local checks do not
prove a remote CI run occurred.

## GitHub publication

The repositories are [DVD2HEVC](https://github.com/nathanturk2/DVD2HEVC) and
[BD2HEVC](https://github.com/nathanturk2/BD2HEVC). Review the final source revision,
author identity and artifact SHA-256 values before pushing. These versions use
`v0.2.0a1` for DVD and `v0.2.0a2` for BD; keep release tags immutable and use a
new version for a subsequent release. Verify the hosted CI run for that revision.
Upload matching source ZIPs, wheels/sdists and release notes as alpha prereleases.

The README and CHANGELOG describe the public behavior. Keep the GPL license,
upstream VM attribution and Hadris MIT source/provenance together. External
VLC/Java/FFmpeg/tsMuxeR binaries are not bundled. A binary distribution of the
legacy patched VLC needs its own corresponding source/licensing materials.
Ask reporters for reviewed diagnostic bundles and precise failure routes,
not media or keys. The separately prepared Reddit draft is not a posted message.
