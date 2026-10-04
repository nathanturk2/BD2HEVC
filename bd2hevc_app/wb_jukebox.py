"""Scoped Warner jukebox repair, retaining the disc's original audio and video."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any

from .navigation import parse_mpls_play_items
from .tools import ToolError
from ._wb_jukebox_payload import CLASSES

STATE = "com.wb.bdj.controller.BD2HEVCMusicJukeboxState"
CLIPS = ("00032", "00019", "00037", "00039", "00038", "00040")
ORIGINAL = {
    "com/wb/bdj/controller/b.class": "e93dd33a81d6c517ac28679b3996ad49364e7d5ea5b0e955a891d4acedae2d9b",
    "com/wb/bdj/controller/MusicJukeboxState.class": "1502182a3e0bc1ab594e07ea5da0f82d7ceb226f3b415524535bd609ac82018d",
    "com/wb/bdj/menu/MusicJukeboxButtonHelper.class": "3cab5db1a9803e22a56fd49ad9cd5cc8d70054165d749575284c10c08154e4d7",
    "com/wb/bdj/menu/k.class": "bb585b2b2e33848e058b6190ea9bbbb7e9a571ab7cf386a776bfff59a2ee80a2",
}
LEGACY = {
    "com/wb/bdj/controller/b.class": "629abe747960480e18ceedd09204e05ece17692798ee6ebfd8937314f1edb6fc",
    "com/wb/bdj/controller/MusicJukeboxState.class": "7a717705fd9cbb916a02f9f7fd287aac865a7c5e10c62b2590d482cfd6fc1eef",
    "com/wb/bdj/menu/MusicJukeboxButtonHelper.class": "1537bfc3ed7a7d77ab77eeb7f819e537401d5cd7ce5842c1f14096045c0c7df1",
    "com/wb/bdj/menu/k.class": "8cc319a86ed94f15295aa5dbc7483268f23a72a5a4a96296304103aaff2d17fb",
}

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def properties(data: bytes) -> dict[str, str]:
    return dict(re.findall(r"(?m)^\s*([^#!\s=]+)\s*=\s*([^\r\n]*)", data.decode("latin1")))

def matches_disc(root: Path) -> bool:
    prop = root / "BDMV/JAR/15631/states.prop"
    if not prop.is_file():
        return False
    values = properties(prop.read_bytes())
    return (values.get("music_jukebox.numberOfTracks") == "41"
            and values.get("music_jukebox.streamIds") == "51"
            and tuple(item["clip_id"] for item in parse_mpls_play_items(root / "BDMV/PLAYLIST/00051.mpls")) == CLIPS)

def single_clip_playlists(data: bytes) -> tuple[list[bytes], list[int]]:
    """Keep each original PlayItem/STN; give it its own playlist and start mark."""
    u16 = lambda pos: int.from_bytes(data[pos:pos + 2], "big")
    u32 = lambda pos: int.from_bytes(data[pos:pos + 4], "big")
    if data[:4] != b"MPLS":
        raise ToolError("Jukebox playlist is not MPLS")
    start = u32(8)
    if start < 40 or start + 10 > len(data) or u16(start + 6) != 6 or u16(start + 8):
        raise ToolError("Unexpected jukebox playlist topology")
    pos = start + 10
    elapsed = 0
    outputs, offsets = [], []
    for expected_clip in CLIPS:
        end = pos + 2 + u16(pos)
        item = data[pos:end]
        if end > len(data) or len(item) < 22 or item[2:7].decode("ascii") != expected_clip:
            raise ToolError("Unexpected jukebox PlayItem")
        offsets.append((elapsed * 1_000_000_000 + 22_500) // 45_000)
        elapsed += u32(pos + 18) - u32(pos + 14)
        body = b"\0\0" + struct.pack(">HH", 1, 0) + item
        mark = b"\0\1" + struct.pack(">H", 0) + item[14:18] + struct.pack(">HI", 65535, 0)
        marks = struct.pack(">H", 1) + mark
        header = bytearray(data[:start])
        header[12:16] = struct.pack(">I", start + 4 + len(body))
        header[16:20] = b"\0" * 4
        outputs.append(bytes(header) + struct.pack(">I", len(body)) + body + struct.pack(">I", len(marks)) + marks)
        pos = end
    return outputs, offsets

def patched_jar(path: Path) -> tuple[bytes, list[str]]:
    payload = {name: base64.b64decode(value) for name, value in CLASSES.items()}
    recovered = []
    replacements = dict(payload)
    with zipfile.ZipFile(path) as archive:
        for name, expected in ORIGINAL.items():
            current = archive.read(name)
            if digest(current) == expected or current == payload.get(name):
                continue
            if digest(current) != LEGACY[name]:
                raise ToolError(f"Unrecognized jukebox class; refusing to overwrite {path}:{name}")
            found = None
            for backup in sorted(path.parent.glob(path.name + ".bak*")):
                with zipfile.ZipFile(backup) as saved:
                    if name in saved.namelist() and digest(saved.read(name)) == expected:
                        found = saved.read(name)
                        break
            if found is None:
                raise ToolError(f"Original class backup is required to remove the legacy jukebox trial: {name}")
            if name not in payload:
                replacements[name] = found
            recovered.append(name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
            for info in archive.infolist():
                name = info.filename
                if name.upper().startswith("META-INF/") and name.upper().endswith((".SF", ".RSA", ".DSA", ".EC")):
                    continue
                output.writestr(info, replacements.pop(name, archive.read(name)))
            for name, content in sorted(replacements.items()):
                info = zipfile.ZipInfo(name, (2026, 9, 6, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                output.writestr(info, content)
        return buffer.getvalue(), recovered

def patch_disc(root: Path, *, backup: bool = True, dry_run: bool = False) -> dict[str, Any]:
    if not matches_disc(root):
        raise ToolError("This jukebox repair does not match the disc's authored track and clip layout")
    bdmv = root / "BDMV"
    playlists, offsets = single_clip_playlists((bdmv / "PLAYLIST/00051.mpls").read_bytes())
    prop = bdmv / "JAR/15631/states.prop"
    data = prop.read_bytes()
    values = properties(data)
    if values.get("music_jukebox.class") not in (STATE, "com.wb.bdj.controller.MusicJukeboxState"):
        raise ToolError("Unexpected jukebox state class")
    ids = list(range(1801, 1807))
    own_playlists = values.get("music_jukebox.bd2hevc.playlists") == ",".join(map(str, ids))
    edits = []
    for folder in ("PLAYLIST", "BACKUP/PLAYLIST"):
        for playlist_id, content in zip(ids, playlists):
            path = bdmv / folder / f"{playlist_id:05}.mpls"
            if path.exists() and path.read_bytes() != content and not own_playlists:
                raise ToolError(f"Jukebox playlist number already in use: {path}")
            edits.append((path, content))
    text = data.decode("latin1")
    newline = "\r\n" if "\r\n" in text else "\n"
    text = re.sub(r"(?m)^music_jukebox\.class=[^\r\n]*", "music_jukebox.class=" + STATE, text)
    text = re.sub(r"(?m)^music_jukebox\.bd2hevc\.[^\r\n]*(?:\r?\n|$)", "", text).rstrip("\r\n") + newline
    text += "music_jukebox.bd2hevc.playlists=" + ",".join(map(str, ids)) + newline
    text += "music_jukebox.bd2hevc.offsets=" + ",".join(map(str, offsets)) + newline
    edits.append((prop, text.encode("latin1")))
    recovered = []
    for path in (bdmv / "JAR/00000.jar", bdmv / "BACKUP/JAR/00000.jar"):
        if path.is_file():
            content, originals = patched_jar(path)
            recovered.extend(originals)
            edits.append((path, content))
    changes = [(path, content) for path, content in edits if not path.exists() or path.read_bytes() != content]
    report = {"patch": "music-jukebox-stable-playback-v2", "target": str(root), "dry_run": dry_run,
              "patched": bool(changes), "already_patched": not changes, "recovered_legacy_classes": recovered,
              "files": [{"path": str(path), "sha256": digest(content)} for path, content in changes],
              "music_playlists": ids, "media_files_changed": 0}
    if not dry_run:
        for path, content in changes:
            path.parent.mkdir(parents=True, exist_ok=True)
            saved = path.with_name(path.name + ".bak_before_jukebox_v2")
            if backup and path.exists() and not saved.exists():
                shutil.copy2(path, saved)
            temp = path.with_name(path.name + ".jukebox-v2.tmp")
            temp.write_bytes(content)
            temp.replace(path)
    return report
