# ISO compatibility and navigation repair

The bundled UDF author version `2.2.0-bd2hevc.5` fixes two descriptor defects:
the logical volume's character-set specification was blank, and terminating
descriptors declared a zero-length CRC body instead of covering their reserved
496 bytes. Both the main and reserve descriptor sequences are corrected.
Together these fixes make existing BD2HEVC images readable through Windows UDF;
changing the UDF revision or partition-map type is not necessary for this fix.

New ISOs pass an independent descriptor check before publication. This check
also runs during `verify-iso`, alongside the bundled reader's allocation checks.

## Existing backups

From the BD2HEVC project directory, inspect a converted ISO without changing it:

```powershell
python -m bd2hevc_app.backup_repair "D:\Backups\Movie.iso" --report inspection.json
```

Apply the inspected types of repair, preserving original bytes in undo journals:

```powershell
python -m bd2hevc_app.backup_repair "D:\Backups\Movie.iso" --apply --journal-dir "D:\Backup repair journals" --report repair.json
```

The same command accepts a converted Blu-ray folder. `--filesystem-only` limits
inspection/repair to UDF descriptors, and can also check DVD ISOs. DVD2HEVC's
legacy genisoimage/mkisofs images do not exhibit the identified writer defects;
the new default UHD-BD output uses the corrected Hadris fork.

Navigation repair measures each available HEVC stream and reconciles its codec,
resolution, progressive/interlaced format, recognized frame rate, and known
colour properties with primary and backup CLPI/MPLS tables. It matches stream
PIDs and playitem clip IDs instead of searching for fixed AVC/MPEG-2 byte strings.
Copied non-HEVC streams retain their source navigation attributes. Missing clips
and unresolved probes are listed in the report; available clips can still be
repaired. Unsupported UDF layouts or corrupt descriptor checksums are refused.

`--assume-bd-sdr` is optional: it labels untagged 8-bit HD BD conversions as
BT.709 SDR and records each inference in the report. Without that option, an
unspecified stream colour space remains unspecified. An explicit non-BT.709
source signal is never replaced with this assumption.

The repair does not resize an ISO or modify M2TS payloads. It acquires an
exclusive writer handle, checks the inspected file identity and original bytes,
durably saves a journal, then writes and reads back the affected regions. Each
ISO has one transaction; a folder has a transaction per changed metadata file.
Keep all journals until you are satisfied with playback. To undo one journal:

```powershell
python -m bd2hevc_app.backup_repair "D:\Backup repair journals\journal.json" --restore
```

Undo refuses to overwrite bytes that differ from both the original and repaired
values. If a folder repair was interrupted, its completed file transactions
remain recoverable individually. ISOs must be unmounted while repairing.

## Future conversion metadata

Source scans retain colour primaries, transfer, matrix and range. Encoder options
and frame properties carry known values through filtering into the HEVC SPS;
setting codec options alone proved insufficient with the bundled FFmpeg build.
After muxing, navigation is reconciled with the actual output streams, including
VC-1 conversions and deinterlaced material. Unknown colour information is not
silently invented by normal conversion.

These changes repair filesystem readability and inaccurate navigation metadata.
They do not turn existing 8-bit HEVC, SD HEVC or unsupported frame rates into
fully compliant UHD-BD video, reconstruct missing clips, or certify hardware
player compatibility. Existing untagged SPS data is unchanged by in-place repair.
Bit-depth/profile changes or adding missing SPS fields require a separate media
remux/re-encode rather than this size-preserving repair.

Descriptor parsing follows libbluray's [CLPI parser](https://raw.githubusercontent.com/ShiftMediaProject/libbluray/master/src/libbluray/bdnav/clpi_parse.c)
and [playlist parser](https://raw.githubusercontent.com/ShiftMediaProject/libbluray/master/src/libbluray/bdnav/mpls_parse.c).
