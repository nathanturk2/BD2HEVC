# Bundled UDF Author

`bin/hadris-udf.exe` is the BD2HEVC streaming fork of Hadris UDF CLI 2.2.0,
used for UDF 2.50 Blu-ray ISO authoring and verification.

- Source: https://github.com/hxyulin/hadris
- Crate: `hadris-udf-cli` 2.2.0
- License: MIT (see `../../THIRD_PARTY_NOTICES.md`)
- Fork source: `streaming-src/`
- Fork version: `2.2.0-bd2hevc.5`
- Windows x64 SHA-256:
  `CE2DD3020CAF386772FCC7E74CDEFFB5E26178BE5A821B470B10722068E24943`

The fork makes the following correctness and performance changes required for
Blu-ray:

- logical-volume descriptors declare OSTA Compressed Unicode, and terminating
  descriptors cover their complete reserved body in the CRC; these are required
  for the generated images to be readable through Windows UDF;
- source files are streamed through a bounded three-buffer pipeline instead of
  loading an entire disc into memory; one sequential reader can stay busy on
  the source drive while one sequential writer drains data to the destination;
- optional source-byte progress sidecars report actual payload consumed rather
  than the misleading full length of the preallocated image;
- NTFS output uses sparse staging so forward metadata seeks do not physically
  zero-fill large gaps before the real Blu-ray payload replaces them; the final
  logical image length is explicitly retained;
- files larger than the UDF short-extent limit are represented by multiple
  contiguous allocation descriptors, and verification checks that descriptors
  cover every declared file byte.

The checked-in binary is built from the vendored fork with Rust:

```powershell
cd tools/hadris-udf/streaming-src/hadris-udf-cli-2.2.0
$buildProfile = $env:USERPROFILE
$posixBuildProfile = $buildProfile.Replace('\', '/')
$env:CARGO_ENCODED_RUSTFLAGS = "--remap-path-prefix=$buildProfile=/build" + [char]31 + "--remap-path-prefix=$posixBuildProfile=/build"
cargo build --release --locked --bin hadris-udf
```

BD2HEVC also accepts a separately installed compatible binary through
`--iso-author-tool` or `BD2HEVC_UDF_TOOL`.

The 5 October 2026 public-release build remaps local build paths to `/build`;
no developer workstation path is embedded in the distributed executable.

Resolved Rust dependency notices and original license texts are included in
`dependency-licenses/`, indexed by `DEPENDENCY-LICENSES.json`. The inventory
includes optional/development/other-platform packages as well as binary inputs.
