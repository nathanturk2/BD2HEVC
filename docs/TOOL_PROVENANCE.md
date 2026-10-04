# Tool source and build inventory

## Bundled UDF author

- Upstream: [Hadris](https://github.com/hxyulin/hadris), revision `9cdad525eede26e5cf720734ca436c51f24dad08`, crates 2.2.0.
- Local fork: `2.2.0-bd2hevc.5`; complete modified source in `tools/hadris-udf/streaming-src`.
- Licence: MIT, included as `tools/hadris-udf/LICENSE-MIT` and in `THIRD_PARTY_NOTICES.md`.
- Toolchain: Rust `1.98.0`, pinned in `rust-toolchain.toml`; Windows x64 MSVC target.
- Build: from the vendored CLI crate, `cargo build --release --locked --bin hadris-udf`.
- Exact binary/source/lockfile SHA-256 values: [BUILD-PROVENANCE.json](../tools/hadris-udf/BUILD-PROVENANCE.json).

A fresh separate target-directory public build completed on 5 October 2026 with workstation paths remapped to `/build`. The exact compiler flags are recorded in BUILD-PROVENANCE.json. Its executable replaces the previous bundled author. `python tools/check-provenance.py` verifies its source inventory and binary hash; CI builds/tests the vendored fork on Windows and Linux.

The streaming fork changes are described in the [author README](../tools/hadris-udf/README.md). No upstream binary is substituted for the local fork.

## External tools

FFmpeg/FFprobe, tsMuxeR, VLC/libbluray, Java and optional MakeMKV are supplied separately. Tool/encoder discovery records the selected paths; release playback evidence should record the exact versions and encoder. Licensing/build flags belong to the selected distributions. The source release does not bundle these programs.

## Release artifacts

`tools/build-release.py` records a positive source inventory and Git revision, then builds wheel/sdist from the same export. `tools/check-artifacts.py` checks every ZIP member, runs exported tests, builds the Windows launcher and verifies a fresh installed wheel outside the checkout. `dist/artifact-check.json` records hashes and results.
