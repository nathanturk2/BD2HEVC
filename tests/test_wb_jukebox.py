import hashlib
import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bd2hevc_app import wb_jukebox as fix
from bd2hevc_app.navigation import parse_mpls_play_items
from bd2hevc_app.tools import ToolError


def playlist_fixture():
    items = []
    for index, clip in enumerate(fix.CLIPS):
        payload = clip.encode() + b"M2TS" + b"\0\1\0" + struct.pack(">II", 27000000, 27045000 + index)
        payload += b"authored-stream-table" + bytes([index])
        items.append(struct.pack(">H", len(payload)) + payload)
    body = b"\0\0" + struct.pack(">HH", 6, 0) + b"".join(items)
    header = b"MPLS0200" + struct.pack(">III", 40, 44 + len(body), 0) + bytes(20)
    return header + struct.pack(">I", len(body)) + body + struct.pack(">IH", 2, 0), items


class JukeboxRepairTests(unittest.TestCase):
    def test_single_playlists_preserve_all_authored_stream_tables(self):
        source, items = playlist_fixture()
        outputs, offsets = fix.single_clip_playlists(source)
        self.assertEqual(len(outputs), 6)
        self.assertEqual(offsets[:3], [0, 1000000000, 2000022222])
        with tempfile.TemporaryDirectory() as folder:
            for output, item, clip in zip(outputs, items, fix.CLIPS):
                path = Path(folder) / "test.mpls"
                path.write_bytes(output)
                self.assertEqual(parse_mpls_play_items(path)[0]["clip_id"], clip)
                self.assertEqual(output[50:50 + len(item)], item)
                mark_start = int.from_bytes(output[12:16], "big")
                self.assertEqual(output[mark_start + 6:mark_start + 10], b"\0\1\0\0")

    def test_rejects_changed_topology(self):
        source, _ = playlist_fixture()
        changed = bytearray(source)
        changed[48:50] = b"\0\1"
        with self.assertRaises(ToolError):
            fix.single_clip_playlists(bytes(changed))

    def test_jar_preserves_unrelated_entries_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "00000.jar"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("original.class", b"original")
                archive.writestr("unrelated.bin", b"keep exactly")
                archive.writestr("META-INF/DISC.RSA", b"invalidated signature")
            with patch.dict(fix.ORIGINAL, {"original.class": fix.digest(b"original")}, clear=True):
                output, recovered = fix.patched_jar(path)
                self.assertEqual(recovered, [])
                with zipfile.ZipFile(io.BytesIO(output)) as archive:
                    self.assertEqual(archive.read("unrelated.bin"), b"keep exactly")
                    self.assertNotIn("META-INF/DISC.RSA", archive.namelist())
                    self.assertTrue(set(fix.CLASSES) <= set(archive.namelist()))
                path.write_bytes(output)
                self.assertEqual(fix.patched_jar(path)[0], output)

    def test_unknown_class_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "00000.jar"
            with zipfile.ZipFile(path, "w") as archive:
                for name in fix.ORIGINAL:
                    archive.writestr(name, b"unknown implementation")
            with self.assertRaises(ToolError):
                fix.patched_jar(path)

    def test_legacy_recovery_requires_verified_original_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "00000.jar"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("test.class", b"legacy")
            with patch.dict(fix.ORIGINAL, {"test.class": fix.digest(b"original")}, clear=True), \
                 patch.dict(fix.LEGACY, {"test.class": fix.digest(b"legacy")}, clear=True):
                with self.assertRaises(ToolError):
                    fix.patched_jar(path)
                with zipfile.ZipFile(path.with_name(path.name + ".bak_original"), "w") as saved:
                    saved.writestr("test.class", b"original")
                output, recovered = fix.patched_jar(path)
                self.assertEqual(recovered, ["test.class"])
                with zipfile.ZipFile(io.BytesIO(output)) as archive:
                    self.assertEqual(archive.read("test.class"), b"original")


if __name__ == "__main__":
    unittest.main()
