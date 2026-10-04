"""BD-J and VLC/libbluray compatibility patch helpers."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_VLC_COMPATIBILITY_MODE,
    HSCENE_MENU_START_SET_VISIBLE,
    KNOWN_VLC_COMPATIBILITY_FIXES,
    VLC_COMPATIBILITY_FIX_ALIASES,
)
from .output import replace_file_with_retry
from .progress import read_text_flexible
from .scan import find_disc_roots
from .tools import ToolError


def clone_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    cloned = zipfile.ZipInfo(info.filename, info.date_time)
    cloned.comment = info.comment
    cloned.extra = info.extra
    cloned.internal_attr = info.internal_attr
    cloned.external_attr = info.external_attr
    cloned.compress_type = info.compress_type
    return cloned


def replace_class_sequence(data: bytes, old: bytes, new: bytes, label: str) -> tuple[bytes, dict[str, Any]]:
    count = data.count(old)
    already = data.count(new) > 0 if old != new else False
    report = {"label": label, "matches": count, "replacement_bytes": len(new), "already_patched": False}
    if count == 0:
        report["already_patched"] = already
        return data, report
    if count != 1:
        report["error"] = f"expected exactly one bytecode match, found {count}"
        return data, report
    return data.replace(old, new, 1), report


def replace_class_sequence_count(
    data: bytes,
    old: bytes,
    new: bytes,
    label: str,
    *,
    expected_matches: int | None = 1,
) -> tuple[bytes, dict[str, Any]]:
    count = data.count(old)
    already = data.count(new) > 0 if old != new else False
    report = {
        "label": label,
        "matches": count,
        "replacement_bytes": len(new),
        "already_patched": count == 0 and already,
    }
    if count == 0:
        return data, report
    if expected_matches is not None and count != expected_matches:
        report["error"] = f"expected {expected_matches} bytecode match(es), found {count}"
        return data, report
    return data.replace(old, new), report


def parse_constant_pool(data: bytes) -> tuple[list[dict[str, Any] | None], int]:
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ToolError("Not a Java class file")
    cp_count = int.from_bytes(data[8:10], "big")
    entries: list[dict[str, Any] | None] = [None] * cp_count
    pos = 10
    index = 1
    while index < cp_count:
        start = pos
        tag = data[pos]
        pos += 1
        entry: dict[str, Any] = {"tag": tag, "start": start}
        if tag == 1:
            length = int.from_bytes(data[pos : pos + 2], "big")
            pos += 2
            raw = data[pos : pos + length]
            pos += length
            entry["value"] = raw.decode("utf-8", errors="replace")
        elif tag in (3, 4):
            pos += 4
        elif tag in (5, 6):
            pos += 8
            entry["end"] = pos
            entry["raw"] = data[start:pos]
            entries[index] = entry
            index += 2
            continue
        elif tag in (7, 8, 16, 19, 20):
            entry["index1"] = int.from_bytes(data[pos : pos + 2], "big")
            pos += 2
        elif tag in (9, 10, 11, 12, 18):
            entry["index1"] = int.from_bytes(data[pos : pos + 2], "big")
            entry["index2"] = int.from_bytes(data[pos + 2 : pos + 4], "big")
            pos += 4
        elif tag == 15:
            entry["ref_kind"] = data[pos]
            entry["index1"] = int.from_bytes(data[pos + 1 : pos + 3], "big")
            pos += 3
        else:
            raise ToolError(f"Unsupported constant pool tag {tag}")
        entry["end"] = pos
        entry["raw"] = data[start:pos]
        entries[index] = entry
        index += 1
    return entries, pos


def skip_class_member(data: bytes, pos: int) -> int:
    pos += 6
    attributes_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(attributes_count):
        pos += 2
        length = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4 + length
    return pos


def insert_in_method_code(
    data: bytes,
    *,
    method_name: str,
    descriptor: str,
    marker: bytes,
    insertion: bytes,
    label: str,
) -> tuple[bytes, dict[str, Any]]:
    entries, pos = parse_constant_pool(data)
    pos += 6
    interfaces_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2 + interfaces_count * 2
    fields_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(fields_count):
        pos = skip_class_member(data, pos)
    methods_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(methods_count):
        name_index = int.from_bytes(data[pos + 2 : pos + 4], "big")
        desc_index = int.from_bytes(data[pos + 4 : pos + 6], "big")
        attributes_count_pos = pos + 6
        attributes_count = int.from_bytes(data[attributes_count_pos : attributes_count_pos + 2], "big")
        attr_pos = attributes_count_pos + 2
        method_matches = cp_utf8(entries, name_index) == method_name and cp_utf8(entries, desc_index) == descriptor
        for _ in range(attributes_count):
            attr_name_index = int.from_bytes(data[attr_pos : attr_pos + 2], "big")
            attr_length_pos = attr_pos + 2
            attr_length = int.from_bytes(data[attr_length_pos : attr_length_pos + 4], "big")
            attr_data_pos = attr_pos + 6
            if method_matches and cp_utf8(entries, attr_name_index) == "Code":
                code_length_pos = attr_data_pos + 4
                code_length = int.from_bytes(data[code_length_pos : code_length_pos + 4], "big")
                code_start = code_length_pos + 4
                code_end = code_start + code_length
                code = data[code_start:code_end]
                if marker + insertion in code:
                    return data, {"label": label, "matches": 0, "already_patched": True, "inserted_bytes": len(insertion)}
                marker_at = code.find(marker)
                if marker_at < 0:
                    return data, {"label": label, "matches": 0, "already_patched": False, "error": "marker not found in method code"}
                insert_at = code_start + marker_at + len(marker)
                new_attr_length = attr_length + len(insertion)
                new_code_length = code_length + len(insertion)
                patched = (
                    data[:attr_length_pos]
                    + new_attr_length.to_bytes(4, "big")
                    + data[attr_length_pos + 4 : code_length_pos]
                    + new_code_length.to_bytes(4, "big")
                    + data[code_length_pos + 4 : insert_at]
                    + insertion
                    + data[insert_at:]
                )
                return patched, {"label": label, "matches": 1, "already_patched": False, "inserted_bytes": len(insertion)}
            attr_pos += 6 + attr_length
        pos = attr_pos
    return data, {"label": label, "matches": 0, "already_patched": False, "error": f"method not found: {method_name}{descriptor}"}


def replace_in_method_code(
    data: bytes,
    *,
    method_name: str,
    descriptor: str,
    old: bytes,
    new: bytes,
    label: str,
    expected_matches: int | None = 1,
) -> tuple[bytes, dict[str, Any]]:
    entries, pos = parse_constant_pool(data)
    pos += 6
    interfaces_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2 + interfaces_count * 2
    fields_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(fields_count):
        pos = skip_class_member(data, pos)
    methods_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(methods_count):
        name_index = int.from_bytes(data[pos + 2 : pos + 4], "big")
        desc_index = int.from_bytes(data[pos + 4 : pos + 6], "big")
        attributes_count_pos = pos + 6
        attributes_count = int.from_bytes(data[attributes_count_pos : attributes_count_pos + 2], "big")
        attr_pos = attributes_count_pos + 2
        method_matches = cp_utf8(entries, name_index) == method_name and cp_utf8(entries, desc_index) == descriptor
        for _ in range(attributes_count):
            attr_name_index = int.from_bytes(data[attr_pos : attr_pos + 2], "big")
            attr_length = int.from_bytes(data[attr_pos + 2 : attr_pos + 6], "big")
            attr_data_pos = attr_pos + 6
            if method_matches and cp_utf8(entries, attr_name_index) == "Code":
                code_length_pos = attr_data_pos + 4
                code_length = int.from_bytes(data[code_length_pos : code_length_pos + 4], "big")
                code_start = code_length_pos + 4
                code_end = code_start + code_length
                code = data[code_start:code_end]
                count = code.count(old)
                already = count == 0 and code.count(new) > 0 if old != new else False
                report = {
                    "label": label,
                    "matches": count,
                    "replacement_bytes": len(new),
                    "already_patched": already,
                }
                if count == 0:
                    if already:
                        return data, report
                    return data, report
                if len(old) != len(new):
                    report["error"] = "method-code replacement must preserve byte length"
                    return data, report
                if expected_matches is not None and count != expected_matches:
                    report["error"] = f"expected {expected_matches} method-code match(es), found {count}"
                    return data, report
                return data[:code_start] + code.replace(old, new) + data[code_end:], report
            attr_pos += 6 + attr_length
        pos = attr_pos
    return data, {"label": label, "matches": 0, "already_patched": False, "error": f"method not found: {method_name}{descriptor}"}


def replace_in_method_code_resized(
    data: bytes,
    *,
    method_name: str,
    descriptor: str,
    old: bytes,
    new: bytes,
    label: str,
    expected_matches: int | None = 1,
    min_max_stack: int | None = None,
    prefer_old_match: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    entries, pos = parse_constant_pool(data)
    pos += 6
    interfaces_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2 + interfaces_count * 2
    fields_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(fields_count):
        pos = skip_class_member(data, pos)
    methods_count = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    for _ in range(methods_count):
        name_index = int.from_bytes(data[pos + 2 : pos + 4], "big")
        desc_index = int.from_bytes(data[pos + 4 : pos + 6], "big")
        attributes_count_pos = pos + 6
        attributes_count = int.from_bytes(data[attributes_count_pos : attributes_count_pos + 2], "big")
        attr_pos = attributes_count_pos + 2
        method_matches = cp_utf8(entries, name_index) == method_name and cp_utf8(entries, desc_index) == descriptor
        for _ in range(attributes_count):
            attr_name_index = int.from_bytes(data[attr_pos : attr_pos + 2], "big")
            attr_length_pos = attr_pos + 2
            attr_length = int.from_bytes(data[attr_length_pos : attr_length_pos + 4], "big")
            attr_data_pos = attr_pos + 6
            if method_matches and cp_utf8(entries, attr_name_index) == "Code":
                code_length_pos = attr_data_pos + 4
                code_length = int.from_bytes(data[code_length_pos : code_length_pos + 4], "big")
                code_start = code_length_pos + 4
                code_end = code_start + code_length
                code = data[code_start:code_end]
                already = code.count(new) > 0 if old != new else False
                if already and not prefer_old_match:
                    return data, {
                        "label": label,
                        "matches": 0,
                        "replacement_bytes": len(new),
                        "already_patched": True,
                    }
                count = code.count(old)
                report = {
                    "label": label,
                    "matches": count,
                    "replacement_bytes": len(new),
                    "already_patched": False,
                }
                if count == 0:
                    return data, report
                if expected_matches is not None and count != expected_matches:
                    report["error"] = f"expected {expected_matches} method-code match(es), found {count}"
                    return data, report
                delta = (len(new) - len(old)) * count
                patched_code = code.replace(old, new)
                patched = bytearray(data)
                if min_max_stack is not None:
                    old_max_stack = int.from_bytes(patched[attr_data_pos : attr_data_pos + 2], "big")
                    patched[attr_data_pos : attr_data_pos + 2] = max(old_max_stack, min_max_stack).to_bytes(2, "big")
                    report["max_stack"] = max(old_max_stack, min_max_stack)
                patched[attr_length_pos : attr_length_pos + 4] = (attr_length + delta).to_bytes(4, "big")
                patched[code_length_pos : code_length_pos + 4] = (code_length + delta).to_bytes(4, "big")
                return bytes(patched[:code_start]) + patched_code + bytes(patched[code_end:]), report
            attr_pos += 6 + attr_length
        pos = attr_pos
    return data, {"label": label, "matches": 0, "already_patched": False, "error": f"method not found: {method_name}{descriptor}"}


def cp_utf8(entries: list[dict[str, Any] | None], index: int) -> str | None:
    entry = entries[index] if 0 < index < len(entries) else None
    return entry.get("value") if entry and entry.get("tag") == 1 else None


def find_cp_utf8(entries: list[dict[str, Any] | None], value: str) -> int | None:
    for index, entry in enumerate(entries):
        if entry and entry.get("tag") == 1 and entry.get("value") == value:
            return index
    return None


def find_cp_class(entries: list[dict[str, Any] | None], class_name: str) -> int | None:
    for index, entry in enumerate(entries):
        if entry and entry.get("tag") == 7 and cp_utf8(entries, int(entry["index1"])) == class_name:
            return index
    return None


def find_cp_name_and_type(entries: list[dict[str, Any] | None], name: str, descriptor: str) -> int | None:
    for index, entry in enumerate(entries):
        if (
            entry
            and entry.get("tag") == 12
            and cp_utf8(entries, int(entry["index1"])) == name
            and cp_utf8(entries, int(entry["index2"])) == descriptor
        ):
            return index
    return None


def find_cp_methodref(entries: list[dict[str, Any] | None], class_name: str, name: str, descriptor: str) -> int | None:
    class_index = find_cp_class(entries, class_name)
    name_type_index = find_cp_name_and_type(entries, name, descriptor)
    if not class_index or not name_type_index:
        return None
    for index, entry in enumerate(entries):
        if entry and entry.get("tag") == 10 and entry.get("index1") == class_index and entry.get("index2") == name_type_index:
            return index
    return None


def find_cp_interface_methodref(entries: list[dict[str, Any] | None], class_name: str, name: str, descriptor: str) -> int | None:
    class_index = find_cp_class(entries, class_name)
    name_type_index = find_cp_name_and_type(entries, name, descriptor)
    if not class_index or not name_type_index:
        return None
    for index, entry in enumerate(entries):
        if entry and entry.get("tag") == 11 and entry.get("index1") == class_index and entry.get("index2") == name_type_index:
            return index
    return None


def find_cp_fieldref(entries: list[dict[str, Any] | None], class_name: str | None, name: str, descriptor: str) -> int | None:
    class_index = find_cp_class(entries, class_name) if class_name else None
    name_type_index = find_cp_name_and_type(entries, name, descriptor)
    if not name_type_index:
        return None
    for index, entry in enumerate(entries):
        if not entry or entry.get("tag") != 9 or entry.get("index2") != name_type_index:
            continue
        if class_index and entry.get("index1") != class_index:
            continue
        return index
    return None


def add_cp_utf8(entries: list[dict[str, Any] | None], additions: list[bytes], value: str) -> int:
    existing = find_cp_utf8(entries, value)
    if existing:
        return existing
    index = len(entries)
    encoded = value.encode("utf-8")
    additions.append(b"\x01" + len(encoded).to_bytes(2, "big") + encoded)
    entries.append({"tag": 1, "value": value})
    return index


def add_cp_class(entries: list[dict[str, Any] | None], additions: list[bytes], class_name: str) -> int:
    existing = find_cp_class(entries, class_name)
    if existing:
        return existing
    name_index = add_cp_utf8(entries, additions, class_name)
    index = len(entries)
    additions.append(b"\x07" + name_index.to_bytes(2, "big"))
    entries.append({"tag": 7, "index1": name_index})
    return index


def add_cp_name_and_type(entries: list[dict[str, Any] | None], additions: list[bytes], name: str, descriptor: str) -> int:
    existing = find_cp_name_and_type(entries, name, descriptor)
    if existing:
        return existing
    name_index = add_cp_utf8(entries, additions, name)
    descriptor_index = add_cp_utf8(entries, additions, descriptor)
    index = len(entries)
    additions.append(b"\x0c" + name_index.to_bytes(2, "big") + descriptor_index.to_bytes(2, "big"))
    entries.append({"tag": 12, "index1": name_index, "index2": descriptor_index})
    return index


def add_cp_methodref(data: bytes, class_name: str, method_name: str, descriptor: str) -> tuple[bytes, int]:
    entries, cp_end = parse_constant_pool(data)
    existing = find_cp_methodref(entries, class_name, method_name, descriptor)
    if existing:
        return data, existing
    class_index = find_cp_class(entries, class_name)
    if not class_index:
        additions: list[bytes] = []
        class_index = add_cp_class(entries, additions, class_name)
    else:
        additions = []
    name_type_index = add_cp_name_and_type(entries, additions, method_name, descriptor)
    methodref_index = len(entries)
    additions.append(b"\x0a" + class_index.to_bytes(2, "big") + name_type_index.to_bytes(2, "big"))
    new_cp_count = int.from_bytes(data[8:10], "big") + len(additions)
    if new_cp_count > 0xFFFF:
        raise ToolError("Constant pool would exceed Java class limit")
    patched = data[:8] + new_cp_count.to_bytes(2, "big") + data[10:cp_end] + b"".join(additions) + data[cp_end:]
    return patched, methodref_index


def add_cp_fieldref(data: bytes, class_name: str, field_name: str, descriptor: str) -> tuple[bytes, int]:
    entries, cp_end = parse_constant_pool(data)
    existing = find_cp_fieldref(entries, class_name, field_name, descriptor)
    if existing:
        return data, existing
    class_index = find_cp_class(entries, class_name)
    if not class_index:
        additions: list[bytes] = []
        class_index = add_cp_class(entries, additions, class_name)
    else:
        additions = []
    name_type_index = add_cp_name_and_type(entries, additions, field_name, descriptor)
    fieldref_index = len(entries)
    additions.append(b"\x09" + class_index.to_bytes(2, "big") + name_type_index.to_bytes(2, "big"))
    new_cp_count = int.from_bytes(data[8:10], "big") + len(additions)
    if new_cp_count > 0xFFFF:
        raise ToolError("Constant pool would exceed Java class limit")
    patched = data[:8] + new_cp_count.to_bytes(2, "big") + data[10:cp_end] + b"".join(additions) + data[cp_end:]
    return patched, fieldref_index


def add_cp_interface_methodref(data: bytes, class_name: str, method_name: str, descriptor: str) -> tuple[bytes, int]:
    entries, cp_end = parse_constant_pool(data)
    existing = find_cp_interface_methodref(entries, class_name, method_name, descriptor)
    if existing:
        return data, existing
    class_index = find_cp_class(entries, class_name)
    if not class_index:
        additions: list[bytes] = []
        class_index = add_cp_class(entries, additions, class_name)
    else:
        additions = []
    name_type_index = add_cp_name_and_type(entries, additions, method_name, descriptor)
    methodref_index = len(entries)
    additions.append(b"\x0b" + class_index.to_bytes(2, "big") + name_type_index.to_bytes(2, "big"))
    new_cp_count = int.from_bytes(data[8:10], "big") + len(additions)
    if new_cp_count > 0xFFFF:
        raise ToolError("Constant pool would exceed Java class limit")
    patched = data[:8] + new_cp_count.to_bytes(2, "big") + data[10:cp_end] + b"".join(additions) + data[cp_end:]
    return patched, methodref_index


def patch_class_methodref_call(
    data: bytes,
    *,
    opcode: int,
    from_class: str,
    from_name: str,
    from_descriptor: str,
    to_class: str,
    to_name: str,
    to_descriptor: str,
    label: str,
    expected_matches: int | None = 1,
) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    old_index = find_cp_methodref(entries, from_class, from_name, from_descriptor)
    if not old_index:
        return data, {"label": label, "matches": 0, "already_patched": False, "error": f"source methodref not found: {from_class}.{from_name}{from_descriptor}"}
    patched, new_index = add_cp_methodref(data, to_class, to_name, to_descriptor)
    old = bytes([opcode]) + old_index.to_bytes(2, "big")
    new = bytes([opcode]) + new_index.to_bytes(2, "big")
    return replace_class_sequence_count(patched, old, new, label, expected_matches=expected_matches)


def patch_jp_menu_start_show(data: bytes) -> tuple[bytes, dict[str, Any]]:
    patched, show_index = add_cp_methodref(data, "org/havi/ui/HScene", "show", "()V")
    replacement = b"\xb2\x00\x27\xb6" + show_index.to_bytes(2, "big") + b"\x00"
    return replace_class_sequence(
        patched,
        HSCENE_MENU_START_SET_VISIBLE,
        replacement,
        "use HScene.show when the main menu starts",
    )


def patch_jp_preserve_scene_on_title(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    scene_field_index = find_cp_fieldref(entries, None, "a", "Lorg/havi/ui/HScene;")
    graphics_field_index = find_cp_fieldref(entries, None, "b", "Ljava/awt/Graphics2D;")
    key_listener_field_index = find_cp_fieldref(entries, None, "c", "Ldt;")
    component_field_index = find_cp_fieldref(entries, None, "d", "Ljava/awt/Component;")
    graphics_dispose_index = find_cp_methodref(entries, "java/awt/Graphics", "dispose", "()V")
    remove_key_listener_index = find_cp_methodref(entries, "java/awt/Component", "removeKeyListener", "(Ljava/awt/event/KeyListener;)V")
    set_visible_index = find_cp_methodref(entries, "org/havi/ui/HScene", "setVisible", "(Z)V")
    scene_dispose_index = find_cp_methodref(entries, "org/havi/ui/HScene", "dispose", "()V")
    if not (
        scene_field_index
        and graphics_field_index
        and key_listener_field_index
        and component_field_index
        and graphics_dispose_index
        and remove_key_listener_index
        and set_visible_index
        and scene_dispose_index
    ):
        return data, {
            "label": "hide instead of dispose HScene when title playback starts",
            "matches": 0,
            "already_patched": False,
            "error": "required HScene cleanup references were not found",
        }
    old = (
        b"\xb2" + scene_field_index.to_bytes(2, "big")
        + b"\xc7\x00\x04"
        + b"\xb1"
        + b"\xb2" + graphics_field_index.to_bytes(2, "big")
        + b"\xc6\x00\x0d"
        + b"\xb2" + graphics_field_index.to_bytes(2, "big")
        + b"\xb6" + graphics_dispose_index.to_bytes(2, "big")
        + b"\x01\xb3" + graphics_field_index.to_bytes(2, "big")
        + b"\xb2" + component_field_index.to_bytes(2, "big")
        + b"\xb2" + key_listener_field_index.to_bytes(2, "big")
        + b"\xb6" + remove_key_listener_index.to_bytes(2, "big")
        + b"\x01\xb3" + key_listener_field_index.to_bytes(2, "big")
        + b"\xb2" + scene_field_index.to_bytes(2, "big")
        + b"\x03\xb6" + set_visible_index.to_bytes(2, "big")
        + b"\xb2" + scene_field_index.to_bytes(2, "big")
        + b"\xb6" + scene_dispose_index.to_bytes(2, "big")
        + b"\x01\xb3" + scene_field_index.to_bytes(2, "big")
        + b"\xb1"
    )
    new_prefix = (
        b"\xb2" + scene_field_index.to_bytes(2, "big")
        + b"\xc7\x00\x04"
        + b"\xb1"
        + b"\xb2" + scene_field_index.to_bytes(2, "big")
        + b"\x03\xb6" + set_visible_index.to_bytes(2, "big")
        + b"\xb1"
    )
    new = new_prefix + (b"\x00" * (len(old) - len(new_prefix)))
    return replace_in_method_code(
        data,
        method_name="a",
        descriptor="()V",
        old=old,
        new=new,
        label="hide instead of dispose HScene when title playback starts",
        expected_matches=1,
    )


def patch_gx_menu_reacquire_graphics_after_show(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    show_index = find_cp_methodref(entries, "jp", "b", "()V")
    if not show_index:
        return data, {
            "label": "reacquire menu graphics after showing scene",
            "matches": 0,
            "already_patched": False,
            "error": "required jp.b() method reference was not found",
        }
    patched, refresh_index = add_cp_methodref(data, "jp", "d", "()V")
    marker = b"\xb8" + show_index.to_bytes(2, "big")
    insertion = b"\xb8" + refresh_index.to_bytes(2, "big")
    return insert_in_method_code(
        patched,
        method_name="H",
        descriptor="()V",
        marker=marker,
        insertion=insertion,
        label="reacquire menu graphics after showing scene",
    )


def patch_menu_remote_show_repaint(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    get_instance_index = find_cp_methodref(entries, "org/havi/ui/HSceneFactory", "getInstance", "()Lorg/havi/ui/HSceneFactory;")
    get_scene_index = find_cp_methodref(entries, "org/havi/ui/HSceneFactory", "getDefaultHScene", "()Lorg/havi/ui/HScene;")
    get_component_index = find_cp_methodref(entries, "java/awt/Container", "getComponent", "(I)Ljava/awt/Component;")
    b_v_index = find_cp_methodref(entries, "hm", "bV", "()V")
    if not (get_instance_index and get_scene_index and get_component_index and b_v_index):
        return data, {
            "label": "show and repaint HScene when remote returns to top menu",
            "matches": 0,
            "already_patched": False,
            "error": "required MenuRemote method references were not found",
        }
    patched, show_index = add_cp_methodref(data, "org/havi/ui/HScene", "show", "()V")
    patched, repaint_index = add_cp_methodref(patched, "java/awt/Component", "repaint", "()V")
    marker = b"\xb6" + b_v_index.to_bytes(2, "big")
    insertion = (
        b"\xb8" + get_instance_index.to_bytes(2, "big")
        + b"\xb6" + get_scene_index.to_bytes(2, "big")
        + b"\xb6" + show_index.to_bytes(2, "big")
        + b"\xb8" + get_instance_index.to_bytes(2, "big")
        + b"\xb6" + get_scene_index.to_bytes(2, "big")
        + b"\x03"
        + b"\xb6" + get_component_index.to_bytes(2, "big")
        + b"\xb6" + repaint_index.to_bytes(2, "big")
    )
    return insert_in_method_code(
        patched,
        method_name="requestFocusAndPlayTopMenu",
        descriptor="()V",
        marker=marker,
        insertion=insertion,
        label="show and repaint HScene when remote returns to top menu",
    )


def patch_topmenu_mark_zero_on_return(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    playlist_field = find_cp_fieldref(entries, "dn", "b", "Laq;")
    pending_mark_field = find_cp_fieldref(entries, "dn", "j", "I")
    playlist_type_method = find_cp_methodref(entries, "aq", "b", "()B")
    clear_resume_method = find_cp_methodref(entries, "dn", "P", "()V")
    playlist_id_method = find_cp_methodref(entries, "ae", "f", "()I")
    play_mark_method = find_cp_methodref(entries, "bd", "a", "(IILlb;)V")
    if not (playlist_field and pending_mark_field and playlist_type_method and clear_resume_method and playlist_id_method and play_mark_method):
        return data, {
            "label": "normalize top-menu playlist return mark",
            "matches": 0,
            "already_patched": False,
            "error": "required BlueMoon top-menu playlist references were not found",
        }
    old = (
        b"\x2a\x1b\xb5" + pending_mark_field.to_bytes(2, "big")
        + b"\x2a\xb6" + clear_resume_method.to_bytes(2, "big")
        + b"\x2a\xb6" + playlist_id_method.to_bytes(2, "big")
        + b"\x1b\x2a\xb8" + play_mark_method.to_bytes(2, "big")
        + b"\xb1"
    )
    guard = (
        b"\x2a\xb4" + playlist_field.to_bytes(2, "big")
        + b"\xb6" + playlist_type_method.to_bytes(2, "big")
        + b"\x05\xa0\x00\x09"
        + b"\x1b\x9e\x00\x05"
        + b"\x03\x3c"
    )
    return replace_in_method_code_resized(
        data,
        method_name="g",
        descriptor="(I)V",
        old=old,
        new=guard + old,
        label="normalize top-menu playlist return mark",
        expected_matches=1,
    )


def patch_music_jukebox_button_queues_state(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    x_instance_method = find_cp_methodref(entries, "com/wb/bdj/controller/x", "a", "()Lcom/wb/bdj/controller/x;")
    direct_change_method = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/x",
        "a",
        "(Ljava/lang/String;Lcom/wb/bdj/controller/q;)V",
    )
    state_field = find_cp_fieldref(entries, "com/wb/bdj/menu/SpecialFeatureClipButtonHelper", "a", "Ljava/lang/String;")
    be_class = find_cp_class(entries, "com/wb/bdj/menu/be")
    jukebox_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "y", "Lcom/wb/bdj/menu/k;")
    playlist_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "z", "Lcom/wb/bdj/menu/k;")
    add_menu_method = find_cp_interface_methodref(entries, "com/wb/bdj/menu/am", "a", "(Lcom/wb/bdj/menu/k;)V")
    if not (
        x_instance_method
        and direct_change_method
        and state_field
        and be_class
        and jukebox_menu_field
        and playlist_menu_field
        and add_menu_method
    ):
        return data, {
            "label": "close previous menu stack and queue music jukebox state after menu additions",
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox references were not found",
        }
    patched, queued_change_method = add_cp_interface_methodref(
        data,
        "com/wb/bdj/menu/am",
        "a",
        "(Ljava/lang/String;Lcom/wb/bdj/controller/q;)V",
    )
    patched, focus_button_method = add_cp_interface_methodref(
        patched,
        "com/wb/bdj/menu/am",
        "a",
        "(Lcom/wb/bdj/menu/b;)V",
    )
    patched, close_menu_method = add_cp_interface_methodref(patched, "com/wb/bdj/menu/am", "c", "()V")
    patched, show_menu_method = add_cp_interface_methodref(patched, "com/wb/bdj/menu/am", "a", "()V")
    patched, default_button_method = add_cp_methodref(patched, "com/wb/bdj/menu/k", "h", "()Lcom/wb/bdj/menu/b;")
    old = (
        b"\xb8" + x_instance_method.to_bytes(2, "big")
        + b"\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\x01\xb6" + direct_change_method.to_bytes(2, "big")
        + b"\x2b\xc1" + be_class.to_bytes(2, "big")
        + b"\x99\x00\x1d"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\xb1"
    )
    menu_additions = (
        b"\x2b\xc1" + be_class.to_bytes(2, "big")
        + b"\x99\x00\x1d"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
    )
    popup_menu_addition = (
        b"\x2b\xc1" + be_class.to_bytes(2, "big")
        + b"\x99\x00\x10"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
    )
    focused_menu_additions = (
        b"\x2b\xc1" + be_class.to_bytes(2, "big")
        + b"\x99\x00\x2d"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb9" + add_menu_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb6" + default_button_method.to_bytes(2, "big")
        + b"\xb9" + focus_button_method.to_bytes(2, "big") + b"\x02\x00"
    )
    state_change = (
        b"\x2c\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\x01\xb9" + queued_change_method.to_bytes(2, "big") + b"\x03\x00"
    )
    focus_after_state_change = (
        b"\x2b\xc1" + be_class.to_bytes(2, "big")
        + b"\x99\x00\x13"
        + b"\x2c\x2b\xc0" + be_class.to_bytes(2, "big")
        + b"\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb6" + default_button_method.to_bytes(2, "big")
        + b"\xb9" + focus_button_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\xb1"
    )
    queued_state_change = (
        menu_additions
        + state_change
        + b"\xb1"
    )
    focused_queued_state_change = (
        focused_menu_additions
        + state_change
        + b"\xb1"
    )
    queued_state_then_focus_change = menu_additions + state_change + focus_after_state_change
    popup_queued_state_then_focus_change = popup_menu_addition + state_change + focus_after_state_change
    cleanup = (
        b"\x2c\xb9" + close_menu_method.to_bytes(2, "big") + b"\x01\x00"
        + b"\x2c\xb9" + show_menu_method.to_bytes(2, "big") + b"\x01\x00"
    )
    menu_first_target = cleanup + focused_queued_state_change
    target = cleanup + state_change + focused_menu_additions + b"\xb1"
    label = "queue music jukebox playlist state before drawing and focusing its popup/group"
    updated, report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=old,
        new=target,
        label=label,
        expected_matches=1,
    )
    if report.get("matches") or report.get("already_patched") or report.get("error"):
        return updated, report
    updated, menu_first_upgrade_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=menu_first_target,
        new=target,
        label=label,
        expected_matches=1,
    )
    if (
        menu_first_upgrade_report.get("matches")
        or menu_first_upgrade_report.get("already_patched")
        or menu_first_upgrade_report.get("error")
    ):
        menu_first_upgrade_report["upgraded_previous_patch"] = bool(menu_first_upgrade_report.get("matches"))
        return updated, menu_first_upgrade_report
    updated, focused_upgrade_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=cleanup + queued_state_change,
        new=target,
        label=label,
        expected_matches=1,
    )
    if focused_upgrade_report.get("matches") or focused_upgrade_report.get("already_patched") or focused_upgrade_report.get("error"):
        focused_upgrade_report["upgraded_previous_patch"] = bool(focused_upgrade_report.get("matches"))
        return updated, focused_upgrade_report
    updated, post_state_focus_upgrade_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=cleanup + queued_state_then_focus_change,
        new=target,
        label=label,
        expected_matches=1,
    )
    if (
        post_state_focus_upgrade_report.get("matches")
        or post_state_focus_upgrade_report.get("already_patched")
        or post_state_focus_upgrade_report.get("error")
    ):
        post_state_focus_upgrade_report["upgraded_previous_patch"] = bool(post_state_focus_upgrade_report.get("matches"))
        return updated, post_state_focus_upgrade_report
    updated, popup_only_upgrade_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=cleanup + popup_queued_state_then_focus_change,
        new=target,
        label=label,
        expected_matches=1,
    )
    if (
        popup_only_upgrade_report.get("matches")
        or popup_only_upgrade_report.get("already_patched")
        or popup_only_upgrade_report.get("error")
    ):
        popup_only_upgrade_report["upgraded_previous_patch"] = bool(popup_only_upgrade_report.get("matches"))
        return updated, popup_only_upgrade_report
    updated, upgrade_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/menu/l;Lcom/wb/bdj/menu/am;)V",
        old=queued_state_change,
        new=target,
        label=label,
        expected_matches=1,
    )
    if upgrade_report.get("matches") or upgrade_report.get("already_patched") or upgrade_report.get("error"):
        upgrade_report["upgraded_previous_patch"] = bool(upgrade_report.get("matches"))
        return updated, upgrade_report
    report["previous_patch_matches"] = upgrade_report.get("matches", 0)
    return patched, report


def patch_music_jukebox_state_restores_default_focus(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    current_button_method = find_cp_interface_methodref(entries, "com/wb/bdj/menu/am", "b", "()Lcom/wb/bdj/menu/b;")
    focus_button_method = find_cp_interface_methodref(entries, "com/wb/bdj/menu/am", "a", "(Lcom/wb/bdj/menu/b;)V")
    helper_method = find_cp_methodref(entries, "com/wb/bdj/menu/l", "w", "()Lcom/wb/bdj/menu/bk;")
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    if not (current_button_method and helper_method and playlist_menu_class):
        return data, {
            "label": "restore music jukebox default focus before key handling",
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox state references were not found",
        }
    patched = data
    if not focus_button_method:
        patched, focus_button_method = add_cp_interface_methodref(
            patched,
            "com/wb/bdj/menu/am",
            "a",
            "(Lcom/wb/bdj/menu/b;)V",
        )
    patched, default_button_method = add_cp_methodref(patched, "com/wb/bdj/menu/k", "h", "()Lcom/wb/bdj/menu/b;")
    entries, _ = parse_constant_pool(patched)
    current_button_method = find_cp_interface_methodref(entries, "com/wb/bdj/menu/am", "b", "()Lcom/wb/bdj/menu/b;")
    focus_button_method = find_cp_interface_methodref(entries, "com/wb/bdj/menu/am", "a", "(Lcom/wb/bdj/menu/b;)V")
    helper_method = find_cp_methodref(entries, "com/wb/bdj/menu/l", "w", "()Lcom/wb/bdj/menu/bk;")
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    if not (current_button_method and focus_button_method and helper_method and playlist_menu_class and default_button_method):
        return data, {
            "label": "restore music jukebox default focus before key handling",
            "matches": 0,
            "already_patched": False,
            "error": "could not add required Warner music jukebox state references",
        }
    old = (
        b"\x2c\xb9" + current_button_method.to_bytes(2, "big") + b"\x01\x00"
        + b"\xb6" + helper_method.to_bytes(2, "big")
        + b"\x3a\x04"
    )
    new = (
        b"\x2c\xb9" + current_button_method.to_bytes(2, "big") + b"\x01\x00"
        + b"\x3a\x04"
        + b"\x19\x04\xc7\x00\x13"
        + b"\x2c\x2d\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\xb6" + default_button_method.to_bytes(2, "big")
        + b"\x59\x3a\x04"
        + b"\xb9" + focus_button_method.to_bytes(2, "big") + b"\x02\x00"
        + b"\x19\x04\xc7\x00\x07"
        + b"\x01\xa7\x00\x08"
        + b"\x19\x04\xb6" + helper_method.to_bytes(2, "big")
        + b"\x3a\x04"
    )
    return replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(ILcom/wb/bdj/menu/am;)Z",
        old=old,
        new=new,
        label="restore music jukebox default focus before key handling",
        expected_matches=1,
    )


def patch_music_jukebox_idle_timer_keeps_playlist_visible(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    set_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "b", "(Z)V")
    reset_geometry_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "e", "(Z)V")
    state_component_method = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "c",
        "(Lcom/wb/bdj/controller/MusicJukeboxState;)Ljava/awt/Component;",
    )
    redraw_method = find_cp_methodref(
        entries,
        "com/wb/bdj/menu/i",
        "a",
        "(Lcom/wb/bdj/menu/k;Ljava/awt/Graphics2D;)V",
    )
    label = "keep music jukebox playlist visible when its idle timer redraws"
    if not (playlist_menu_class and set_visible_method and state_component_method and redraw_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox idle-timer references were not found",
        }
    old = (
        b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\x03\xb6" + set_visible_method.to_bytes(2, "big")
    )
    new = (
        b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\x04\xb6" + set_visible_method.to_bytes(2, "big")
    )
    patched, report = replace_in_method_code(
        data,
        method_name="run",
        descriptor="()V",
        old=old,
        new=new,
        label=label,
        expected_matches=1,
    )
    if not (report.get("matches") or report.get("already_patched") or report.get("error")):
        reset_visible = (
            b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
            + b"\x04\xb6" + reset_geometry_method.to_bytes(2, "big")
            if reset_geometry_method
            else None
        )
        if reset_visible and data.count(reset_visible) == 1:
            report["already_patched"] = True
            report["playlist_geometry_reset"] = True
    return patched, report


def patch_music_jukebox_idle_timer_restores_group_geometry(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    set_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "b", "(Z)V")
    label = "restore the music playlist group's authored position before its timer redraw"
    if not (playlist_menu_class and set_visible_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner playlist-group visibility references were not found",
        }
    patched, reset_geometry_method = add_cp_methodref(data, "com/wb/bdj/menu/k", "e", "(Z)V")
    entries, _ = parse_constant_pool(patched)
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    set_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "b", "(Z)V")
    reset_geometry_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "e", "(Z)V")
    if not (playlist_menu_class and set_visible_method and reset_geometry_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "could not add the Warner playlist-group geometry reset reference",
        }
    prefix = b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big") + b"\x04\xb6"
    return replace_in_method_code(
        patched,
        method_name="run",
        descriptor="()V",
        old=prefix + set_visible_method.to_bytes(2, "big"),
        new=prefix + reset_geometry_method.to_bytes(2, "big"),
        label=label,
        expected_matches=1,
    )


def patch_music_jukebox_group_restores_authored_position(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    x_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "o", "I")
    y_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "p", "I")
    set_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "b", "(Z)V")
    label = "make the music playlist redraw hook restore its authored position and visibility"
    if not all((x_field, y_field, set_visible_method)):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner playlist-group geometry fields were not found",
        }
    patched, authored_x_field = add_cp_fieldref(data, "com/wb/bdj/menu/l", "q", "I")
    patched, authored_y_field = add_cp_fieldref(patched, "com/wb/bdj/menu/l", "r", "I")
    entries, _ = parse_constant_pool(patched)
    x_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "o", "I")
    y_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "p", "I")
    authored_x_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "q", "I")
    authored_y_field = find_cp_fieldref(entries, "com/wb/bdj/menu/l", "r", "I")
    set_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "b", "(Z)V")
    if not all((x_field, y_field, authored_x_field, authored_y_field, set_visible_method)):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "could not add the Warner authored-position field references",
        }
    new = (
        b"\x2a\x2a\xb4" + authored_x_field.to_bytes(2, "big")
        + b"\xb5" + x_field.to_bytes(2, "big")
        + b"\x2a\x2a\xb4" + authored_y_field.to_bytes(2, "big")
        + b"\xb5" + y_field.to_bytes(2, "big")
        + b"\x2a\x1b\xb6" + set_visible_method.to_bytes(2, "big")
        + b"\xb1"
    )
    return replace_in_method_code_resized(
        patched,
        method_name="e",
        descriptor="(Z)V",
        old=b"\xb1",
        new=new,
        label=label,
        expected_matches=1,
        min_max_stack=2,
    )


def patch_music_jukebox_idle_timer_restores_graphics_lookup(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    cached_component_method = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "c",
        "(Lcom/wb/bdj/controller/MusicJukeboxState;)Ljava/awt/Component;",
    )
    get_graphics_method = find_cp_methodref(entries, "java/awt/Component", "getGraphics", "()Ljava/awt/Graphics;")
    menu_controller_field = find_cp_fieldref(entries, "com/wb/bdj/controller/g", "e", "Lcom/wb/bdj/menu/ak;")
    current_component_method = find_cp_methodref(entries, "com/wb/bdj/menu/ak", "a", "()Ljava/awt/Component;")
    label = "preserve music jukebox timer component-to-graphics lookup"
    if not (cached_component_method and get_graphics_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox graphics references were not found",
        }
    correct = (
        b"\xb8" + cached_component_method.to_bytes(2, "big")
        + b"\xb6" + get_graphics_method.to_bytes(2, "big")
    )
    if correct in data:
        return data, {
            "label": label,
            "matches": 0,
            "replacement_bytes": len(correct),
            "already_patched": True,
        }
    if not (menu_controller_field and current_component_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "neither the correct nor repairable timer graphics sequence was found",
        }
    incorrect = (
        b"\xb4" + menu_controller_field.to_bytes(2, "big")
        + b"\xb6" + current_component_method.to_bytes(2, "big")
    )
    return replace_in_method_code(
        data,
        method_name="run",
        descriptor="()V",
        old=incorrect,
        new=correct,
        label=label,
        expected_matches=1,
    )


def patch_music_jukebox_idle_timer_draws_incoming_playlist(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Draw the playlist group owned by the newly entered jukebox state.

    The menu transition animates a group from the outgoing SpecialFeaturesState,
    then playlist 51 recreates VLC's video output.  MusicJukeboxState owns a
    separate group instance whose visibility flag starts false.  Warner's idle
    timer tests that flag before setting it and drawing, so the only instance
    capable of repopulating the new VLC subpicture is skipped.  Consume the
    stale flag result and continue into the existing set-visible/redraw block.
    """
    entries, _ = parse_constant_pool(data)
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    is_visible_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "n", "()Z")
    label = "draw the incoming music jukebox playlist after VLC changes output"
    if not (playlist_menu_class and is_visible_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox visibility references were not found",
        }
    old = (
        b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\xb6" + is_visible_method.to_bytes(2, "big")
        + b"\x99\x00\x2f"
    )
    new = (
        b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\xb6" + is_visible_method.to_bytes(2, "big")
        + b"\x57\x00\x00"
    )
    return replace_in_method_code(
        data,
        method_name="run",
        descriptor="()V",
        old=old,
        new=new,
        label=label,
        expected_matches=1,
    )


def patch_music_jukebox_idle_timer_repeats_playlist_redraw(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    long_ten_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry and entry.get("tag") == 5 and int.from_bytes(entry["raw"][1:9], "big", signed=True) == 10
        ),
        None,
    )
    cancel_method = find_cp_methodref(entries, "java/util/TimerTask", "cancel", "()Z")
    state_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/b",
        "a",
        "Lcom/wb/bdj/controller/MusicJukeboxState;",
    )
    clear_task_method = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "a",
        "(Lcom/wb/bdj/controller/MusicJukeboxState;Ljava/util/TimerTask;)V",
    )
    label = "repeat live music jukebox playlist redraws after entry"
    if not (long_ten_index and cancel_method and state_field and clear_task_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox repeating-timer references were not found",
        }
    threshold_old = b"\x14" + long_ten_index.to_bytes(2, "big")
    threshold_previous = b"\x09\x00\x00"
    # playlist 51 takes roughly three seconds to replace VLC's vout.  Drawing
    # before then returns a null Graphics and kills java.util.Timer's thread.
    # iconst_4 + i2l keeps the original three-byte instruction footprint.
    threshold_new = b"\x07\x85\x00"
    cancel_old = b"\x2a\xb6" + cancel_method.to_bytes(2, "big") + b"\x57"
    cancel_new = b"\x2a\xb4" + state_field.to_bytes(2, "big") + b"\x57"
    clear_old = (
        b"\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\x01\xb8" + clear_task_method.to_bytes(2, "big")
    )
    clear_new = (
        b"\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\x59\x57\x57\x00"
    )
    patched, threshold_report = replace_in_method_code(
        data,
        method_name="run",
        descriptor="()V",
        old=threshold_old,
        new=threshold_new,
        label=f"{label}: wait for VLC output replacement",
        expected_matches=1,
    )
    if not (
        threshold_report.get("matches") == 1
        or threshold_report.get("already_patched")
        or threshold_report.get("error")
    ):
        patched, threshold_report = replace_in_method_code(
            patched,
            method_name="run",
            descriptor="()V",
            old=threshold_previous,
            new=threshold_new,
            label=f"{label}: wait for VLC output replacement",
            expected_matches=1,
        )
        threshold_report["upgraded_previous_patch"] = bool(threshold_report.get("matches"))
    reports: list[dict[str, Any]] = [threshold_report]
    if threshold_report.get("error") or not (
        threshold_report.get("matches") == 1 or threshold_report.get("already_patched")
    ):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "parts": reports,
            "error": "could not safely patch repeating timer part: wait for VLC output replacement",
        }
    for old, new, part_label in (
        (cancel_old, cancel_new, "keep redraw task scheduled"),
        (clear_old, clear_new, "retain redraw task handle"),
    ):
        patched, part_report = replace_in_method_code(
            patched,
            method_name="run",
            descriptor="()V",
            old=old,
            new=new,
            label=f"{label}: {part_label}",
            expected_matches=1,
        )
        reports.append(part_report)
        if part_report.get("error") or not (
            part_report.get("matches") == 1 or part_report.get("already_patched")
        ):
            return data, {
                "label": label,
                "matches": 0,
                "already_patched": False,
                "parts": reports,
                "error": f"could not safely patch repeating timer part: {part_label}",
            }
    changed = any(report.get("matches") == 1 for report in reports)
    return patched, {
        "label": label,
        "matches": 1 if changed else 0,
        "already_patched": not changed and all(report.get("already_patched") for report in reports),
        "parts": reports,
    }


def patch_music_jukebox_timer_resubmits_full_argb(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Make the live jukebox timer re-submit the complete BD-J backbuffer.

    VLC 3 can recreate its vout/subpicture channel when playlist 51 starts while
    libbluray's BDRootWindow still considers the ARGB overlay open.  A normal
    header-only repaint then leaves the already-rendered song grid missing.
    copyArea(..., 0, 0) preserves every pixel while marking the full plane dirty.
    """
    label = "resubmit the full music jukebox ARGB plane after VLC vout changes"
    patched, copy_area_method = add_cp_methodref(data, "java/awt/Graphics", "copyArea", "(IIIIII)V")
    entries, pos = parse_constant_pool(patched)
    playlist_menu_class = find_cp_class(entries, "com/wb/bdj/menu/k")
    state_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/b",
        "a",
        "Lcom/wb/bdj/controller/MusicJukeboxState;",
    )
    state_component_method = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "c",
        "(Lcom/wb/bdj/controller/MusicJukeboxState;)Ljava/awt/Component;",
    )
    get_graphics_method = find_cp_methodref(entries, "java/awt/Component", "getGraphics", "()Ljava/awt/Graphics;")
    graphics2d_class = find_cp_class(entries, "java/awt/Graphics2D")
    redraw_method = find_cp_methodref(
        entries,
        "com/wb/bdj/menu/i",
        "a",
        "(Lcom/wb/bdj/menu/k;Ljava/awt/Graphics2D;)V",
    )
    if not (
        playlist_menu_class
        and state_field
        and state_component_method
        and get_graphics_method
        and graphics2d_class
        and redraw_method
        and copy_area_method
    ):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner timer or java.awt graphics references were not found",
        }

    old = (
        b"\x2b\xc0" + playlist_menu_class.to_bytes(2, "big")
        + b"\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\xb8" + state_component_method.to_bytes(2, "big")
        + b"\xb6" + get_graphics_method.to_bytes(2, "big")
        + b"\xc0" + graphics2d_class.to_bytes(2, "big")
        + b"\xb8" + redraw_method.to_bytes(2, "big")
    )
    new = (
        b"\x2a\xb4" + state_field.to_bytes(2, "big")
        + b"\xb8" + state_component_method.to_bytes(2, "big")
        + b"\xb6" + get_graphics_method.to_bytes(2, "big")
        + b"\x03\x03"
        + b"\x11\x07\x80"  # width 1920
        + b"\x11\x04\x38"  # height 1080
        + b"\x03\x03"
        + b"\xb6" + copy_area_method.to_bytes(2, "big")
    )

    pos += 6
    interfaces_count = int.from_bytes(patched[pos : pos + 2], "big")
    pos += 2 + interfaces_count * 2
    fields_count = int.from_bytes(patched[pos : pos + 2], "big")
    pos += 2
    for _ in range(fields_count):
        pos = skip_class_member(patched, pos)
    methods_count = int.from_bytes(patched[pos : pos + 2], "big")
    pos += 2
    for _ in range(methods_count):
        name_index = int.from_bytes(patched[pos + 2 : pos + 4], "big")
        desc_index = int.from_bytes(patched[pos + 4 : pos + 6], "big")
        attributes_count = int.from_bytes(patched[pos + 6 : pos + 8], "big")
        attr_pos = pos + 8
        method_matches = cp_utf8(entries, name_index) == "run" and cp_utf8(entries, desc_index) == "()V"
        for _ in range(attributes_count):
            attr_name_index = int.from_bytes(patched[attr_pos : attr_pos + 2], "big")
            attr_length_pos = attr_pos + 2
            attr_length = int.from_bytes(patched[attr_length_pos : attr_length_pos + 4], "big")
            attr_data_pos = attr_pos + 6
            if method_matches and cp_utf8(entries, attr_name_index) == "Code":
                code_length_pos = attr_data_pos + 4
                code_length = int.from_bytes(patched[code_length_pos : code_length_pos + 4], "big")
                code_start = code_length_pos + 4
                code_end = code_start + code_length
                code = patched[code_start:code_end]
                if new in code:
                    return patched, {"label": label, "matches": 0, "already_patched": True}
                count = code.count(old)
                if count != 1:
                    return data, {
                        "label": label,
                        "matches": count,
                        "already_patched": False,
                        "error": f"expected one live playlist redraw sequence, found {count}",
                    }
                if int.from_bytes(patched[code_end : code_end + 2], "big") != 0:
                    return data, {
                        "label": label,
                        "matches": 0,
                        "already_patched": False,
                        "error": "timer run method unexpectedly has an exception table",
                    }
                rewritten = code.replace(old, new, 1)
                for old_branch, new_branch in (
                    (b"\x9b\x00\x59", b"\x9b\x00\x5c"),
                    (b"\xc6\x00\x40", b"\xc6\x00\x43"),
                    (b"\x99\x00\x39", b"\x99\x00\x3c"),
                    (b"\x99\x00\x2f", b"\x99\x00\x32"),
                ):
                    if rewritten.count(old_branch) != 1:
                        return data, {
                            "label": label,
                            "matches": 0,
                            "already_patched": False,
                            "error": f"expected timer branch {old_branch.hex()} was not unique",
                        }
                    rewritten = rewritten.replace(old_branch, new_branch, 1)
                delta = len(new) - len(old)
                result = bytearray(patched)
                result[attr_length_pos : attr_length_pos + 4] = (attr_length + delta).to_bytes(4, "big")
                result[attr_data_pos : attr_data_pos + 2] = max(
                    7, int.from_bytes(patched[attr_data_pos : attr_data_pos + 2], "big")
                ).to_bytes(2, "big")
                result[code_length_pos : code_length_pos + 4] = (code_length + delta).to_bytes(4, "big")
                output = bytes(result[:code_start]) + rewritten + bytes(result[code_end:])
                return output, {"label": label, "matches": 1, "already_patched": False}
            attr_pos += 6 + attr_length
        pos = attr_pos
    return data, {
        "label": label,
        "matches": 0,
        "already_patched": False,
        "error": "timer run() method was not found",
    }


def patch_music_jukebox_state_uses_current_menu_component(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    cached_component_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "m",
        "Ljava/awt/Component;",
    )
    menu_controller_field = find_cp_fieldref(entries, "com/wb/bdj/controller/g", "e", "Lcom/wb/bdj/menu/ak;")
    label = "draw music jukebox redraws on libbluray's root overlay component"
    if not cached_component_field:
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "the Warner music jukebox cached component field was not found",
        }
    patched, root_overlay_method = add_cp_methodref(
        data,
        "org/videolan/GUIManager",
        "getInstance",
        "()Lorg/videolan/GUIManager;",
    )
    entries, _ = parse_constant_pool(patched)
    cached_component_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "m",
        "Ljava/awt/Component;",
    )
    menu_controller_field = find_cp_fieldref(entries, "com/wb/bdj/controller/g", "e", "Lcom/wb/bdj/menu/ak;")
    root_overlay_method = find_cp_methodref(
        entries,
        "org/videolan/GUIManager",
        "getInstance",
        "()Lorg/videolan/GUIManager;",
    )
    current_component_method = find_cp_methodref(entries, "com/wb/bdj/menu/ak", "a", "()Ljava/awt/Component;")
    show_component_method = find_cp_methodref(entries, "java/awt/Component", "show", "()V")
    hide_component_method = find_cp_methodref(entries, "java/awt/Component", "hide", "()V")
    get_graphics_method = find_cp_methodref(entries, "java/awt/Component", "getGraphics", "()Ljava/awt/Graphics;")
    if not (cached_component_field and root_overlay_method):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "could not add libbluray's root overlay-component reference",
        }
    old = b"\x2a\xb4" + cached_component_field.to_bytes(2, "big") + b"\xb0"
    root_overlay = b"\xb8" + root_overlay_method.to_bytes(2, "big") + b"\xb0"
    previous_versions: list[bytes] = []
    if menu_controller_field and current_component_method:
        current = (
            b"\x2a\xb4" + menu_controller_field.to_bytes(2, "big")
            + b"\xb6" + current_component_method.to_bytes(2, "big")
            + b"\xb0"
        )
        previous_versions.append(current)
    if previous_versions and show_component_method:
        current_show = current[:-1] + b"\xb6" + show_component_method.to_bytes(2, "big") + current
        previous_versions.insert(0, current_show)
        if hide_component_method:
            current_reset = current[:-1] + b"\xb6" + hide_component_method.to_bytes(2, "big") + current_show
            previous_versions.insert(0, current_reset)
            if get_graphics_method:
                previous_versions.insert(
                    0,
                    current[:-1]
                    + b"\xb6" + get_graphics_method.to_bytes(2, "big")
                    + b"\xc7\x00\x17"
                    + current_reset,
                )
    for previous in previous_versions:
        updated, upgrade_report = replace_in_method_code_resized(
            patched,
            method_name="c",
            descriptor="(Lcom/wb/bdj/controller/MusicJukeboxState;)Ljava/awt/Component;",
            old=previous,
            new=root_overlay,
            label=label,
            expected_matches=1,
            prefer_old_match=True,
        )
        if upgrade_report.get("matches") or upgrade_report.get("already_patched") or upgrade_report.get("error"):
            upgrade_report["upgraded_previous_patch"] = bool(upgrade_report.get("matches"))
            return updated, upgrade_report
    return replace_in_method_code_resized(
        patched,
        method_name="c",
        descriptor="(Lcom/wb/bdj/controller/MusicJukeboxState;)Ljava/awt/Component;",
        old=old,
        new=root_overlay,
        label=label,
        expected_matches=1,
    )


def patch_music_jukebox_state_resets_overlay_before_redraw(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    redraw_counter_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "j",
        "J",
    )
    label = "reset the VLC overlay one timer pass before the music playlist redraw"
    if not redraw_counter_field:
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "the Warner music jukebox redraw counter was not found",
        }
    patched, root_overlay_method = add_cp_methodref(
        data,
        "org/videolan/GUIManager",
        "getInstance",
        "()Lorg/videolan/GUIManager;",
    )
    patched, hide_component_method = add_cp_methodref(patched, "java/awt/Component", "hide", "()V")
    patched, show_component_method = add_cp_methodref(patched, "java/awt/Component", "show", "()V")
    entries, _ = parse_constant_pool(patched)
    menu_controller_field = find_cp_fieldref(entries, "com/wb/bdj/controller/g", "e", "Lcom/wb/bdj/menu/ak;")
    redraw_counter_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "j",
        "J",
    )
    root_overlay_method = find_cp_methodref(
        entries,
        "org/videolan/GUIManager",
        "getInstance",
        "()Lorg/videolan/GUIManager;",
    )
    current_component_method = find_cp_methodref(entries, "com/wb/bdj/menu/ak", "a", "()Ljava/awt/Component;")
    hide_component_method = find_cp_methodref(entries, "java/awt/Component", "hide", "()V")
    show_component_method = find_cp_methodref(entries, "java/awt/Component", "show", "()V")
    if not all(
        (redraw_counter_field, root_overlay_method, hide_component_method, show_component_method)
    ):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "could not add the Warner overlay lifecycle references",
        }
    old = b"\x2a\x1f\xb5" + redraw_counter_field.to_bytes(2, "big") + b"\xb1"
    root_overlay = b"\xb8" + root_overlay_method.to_bytes(2, "big")
    new = (
        b"\x1f\x07\x85\x94\x9a\x00\x0f"
        + root_overlay + b"\xb6" + hide_component_method.to_bytes(2, "big")
        + root_overlay + b"\xb6" + show_component_method.to_bytes(2, "big")
        + old[:-1] + b"\xb1"
    )
    if menu_controller_field and current_component_method:
        current = (
            b"\x2a\xb4" + menu_controller_field.to_bytes(2, "big")
            + b"\xb6" + current_component_method.to_bytes(2, "big")
        )
        previous = (
            b"\x1f\x07\x85\x94\x9a\x00\x17"
            + current + b"\xb6" + hide_component_method.to_bytes(2, "big")
            + current + b"\xb6" + show_component_method.to_bytes(2, "big")
            + old[:-1] + b"\xb1"
        )
        upgraded, upgrade_report = replace_in_method_code_resized(
            patched,
            method_name="a",
            descriptor="(Lcom/wb/bdj/controller/MusicJukeboxState;J)V",
            old=previous,
            new=new,
            label=label,
            expected_matches=1,
            min_max_stack=4,
            prefer_old_match=True,
        )
        if upgrade_report.get("matches") or upgrade_report.get("error"):
            upgrade_report["upgraded_child_component_reset"] = bool(upgrade_report.get("matches"))
            return upgraded, upgrade_report
    duplicated = new[: -len(old)] + new
    normalized, duplicate_report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/controller/MusicJukeboxState;J)V",
        old=duplicated,
        new=new,
        label=label,
        expected_matches=1,
        min_max_stack=4,
        prefer_old_match=True,
    )
    if duplicate_report.get("matches") or duplicate_report.get("error"):
        duplicate_report["removed_duplicate_reset"] = bool(duplicate_report.get("matches"))
        return normalized, duplicate_report
    return replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/controller/MusicJukeboxState;J)V",
        old=old,
        new=new,
        label=label,
        expected_matches=1,
        min_max_stack=4,
    )


def patch_music_jukebox_state_schedules_entry_redraw(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    redraw_task_class = find_cp_class(entries, "com/wb/bdj/controller/b")
    redraw_task_constructor = find_cp_methodref(
        entries,
        "com/wb/bdj/controller/b",
        "<init>",
        "(Lcom/wb/bdj/controller/MusicJukeboxState;)V",
    )
    redraw_task_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "l",
        "Ljava/util/TimerTask;",
    )
    timer_field = find_cp_fieldref(
        entries,
        "com/wb/bdj/controller/MusicJukeboxState",
        "k",
        "Ljava/util/Timer;",
    )
    schedule_method = find_cp_methodref(
        entries,
        "java/util/Timer",
        "scheduleAtFixedRate",
        "(Ljava/util/TimerTask;JJ)V",
    )
    error_log_method = find_cp_methodref(
        entries,
        "com/wb/bdj/a/i",
        "b",
        "(Ljava/lang/Object;Ljava/lang/String;)V",
    )
    period_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry and entry.get("tag") == 5 and int.from_bytes(entry["raw"][1:9], "big", signed=True) == 1000
        ),
        None,
    )
    label = "schedule live music jukebox playlist redraws on state entry"
    if not (
        redraw_task_class
        and redraw_task_constructor
        and redraw_task_field
        and timer_field
        and schedule_method
        and error_log_method
        and period_index
    ):
        return data, {
            "label": label,
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox entry-timer references were not found",
        }
    insertion = (
        b"\x2a\xbb" + redraw_task_class.to_bytes(2, "big")
        + b"\x59\x2a\xb7" + redraw_task_constructor.to_bytes(2, "big")
        + b"\xb5" + redraw_task_field.to_bytes(2, "big")
        + b"\x2a\xb4" + timer_field.to_bytes(2, "big")
        + b"\x2a\xb4" + redraw_task_field.to_bytes(2, "big")
        + b"\x09\x14" + period_index.to_bytes(2, "big")
        + b"\xb6" + schedule_method.to_bytes(2, "big")
    )
    marker = b"\xb8" + error_log_method.to_bytes(2, "big")
    return insert_in_method_code(
        data,
        method_name="a",
        descriptor="(Lcom/wb/bdj/controller/q;)V",
        marker=marker,
        insertion=insertion,
        label=label,
    )


def patch_music_jukebox_button_keeps_playlist_group_separate(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    jukebox_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "y", "Lcom/wb/bdj/menu/k;")
    playlist_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "z", "Lcom/wb/bdj/menu/k;")
    if not (jukebox_menu_field and playlist_menu_field):
        return data, {
            "label": "keep music jukebox playlist group as a separate authored layer",
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox menu fields were not found",
        }
    separate = b"\xb5" + playlist_menu_field.to_bytes(2, "big") + b"\xb1"
    if separate in data:
        return data, {
            "label": "keep music jukebox playlist group as a separate authored layer",
            "matches": 0,
            "replacement_bytes": len(separate),
            "already_patched": True,
        }
    patched, children_method = add_cp_methodref(data, "com/wb/bdj/menu/k", "p", "()Ljava/util/ArrayList;")
    patched, add_child_method = add_cp_methodref(patched, "java/util/ArrayList", "add", "(Ljava/lang/Object;)Z")
    patched, set_parent_method = add_cp_methodref(patched, "com/wb/bdj/menu/l", "a", "(Lcom/wb/bdj/menu/k;)V")
    entries, _ = parse_constant_pool(patched)
    jukebox_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "y", "Lcom/wb/bdj/menu/k;")
    playlist_menu_field = find_cp_fieldref(entries, "com/wb/bdj/menu/be", "z", "Lcom/wb/bdj/menu/k;")
    children_method = find_cp_methodref(entries, "com/wb/bdj/menu/k", "p", "()Ljava/util/ArrayList;")
    add_child_method = find_cp_methodref(entries, "java/util/ArrayList", "add", "(Ljava/lang/Object;)Z")
    set_parent_method = find_cp_methodref(entries, "com/wb/bdj/menu/l", "a", "(Lcom/wb/bdj/menu/k;)V")
    if not (jukebox_menu_field and playlist_menu_field and children_method and add_child_method and set_parent_method):
        return data, {
            "label": "keep music jukebox playlist group as a separate authored layer",
            "matches": 0,
            "already_patched": False,
            "error": "could not add required Warner music jukebox group-parent references",
        }
    attached = (
        b"\xb5" + playlist_menu_field.to_bytes(2, "big")
        + b"\x2a\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb6" + children_method.to_bytes(2, "big")
        + b"\x2a\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\xb6" + add_child_method.to_bytes(2, "big")
        + b"\x57"
        + b"\x2a\xb4" + playlist_menu_field.to_bytes(2, "big")
        + b"\x2a\xb4" + jukebox_menu_field.to_bytes(2, "big")
        + b"\xb6" + set_parent_method.to_bytes(2, "big")
        + b"\xb1"
    )
    return replace_in_method_code_resized(
        patched,
        method_name="<init>",
        descriptor="(Ljava/lang/String;Lcom/wb/bdj/menu/k;Ljava/util/Properties;Lcom/wb/bdj/menu/bm;)V",
        old=attached,
        new=separate,
        label="keep music jukebox playlist group as a separate authored layer",
        expected_matches=1,
    )


def patch_music_jukebox_state_stops_after_start_select(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    player_field = find_cp_fieldref(entries, "com/wb/bdj/controller/x", "g", "Ljavax/media/Player;")
    prefetch_method = find_cp_interface_methodref(entries, "javax/media/Controller", "prefetch", "()V")
    if not player_field:
        return data, {
            "label": "stop music jukebox startup playlist after selecting it",
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox player reference was not found",
        }
    patched, stop_method = add_cp_interface_methodref(data, "javax/media/Clock", "stop", "()V")
    entries, _ = parse_constant_pool(patched)
    player_field = find_cp_fieldref(entries, "com/wb/bdj/controller/x", "g", "Ljavax/media/Player;")
    prefetch_method = find_cp_interface_methodref(entries, "javax/media/Controller", "prefetch", "()V")
    stop_method = find_cp_interface_methodref(entries, "javax/media/Clock", "stop", "()V")
    if not (player_field and stop_method):
        return data, {
            "label": "stop music jukebox startup playlist after selecting it",
            "matches": 0,
            "already_patched": False,
            "error": "could not add required Warner music jukebox stop reference",
        }
    new = (
        b"\x2d\xb4" + player_field.to_bytes(2, "big")
        + b"\xb9" + stop_method.to_bytes(2, "big") + b"\x01\x00"
    )
    label = "stop music jukebox startup playlist after selecting it"
    if prefetch_method:
        old = (
            b"\x2d\xb4" + player_field.to_bytes(2, "big")
            + b"\xb9" + prefetch_method.to_bytes(2, "big") + b"\x01\x00"
        )
        updated, report = replace_in_method_code_resized(
            patched,
            method_name="a",
            descriptor="(Lcom/wb/bdj/controller/q;)V",
            old=old,
            new=new,
            label=label,
            expected_matches=1,
        )
        if report.get("matches") or report.get("already_patched") or report.get("error"):
            return updated, report
    old_nops = b"\x00" * len(new)
    updated, report = replace_in_method_code_resized(
        patched,
        method_name="a",
        descriptor="(Lcom/wb/bdj/controller/q;)V",
        old=old_nops,
        new=new,
        label=label,
        expected_matches=1,
    )
    if report.get("matches") or report.get("already_patched") or report.get("error"):
        report["upgraded_previous_patch"] = bool(report.get("matches"))
    return updated, report


def patch_music_jukebox_state_skips_start_prefetch(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    player_field = find_cp_fieldref(entries, "com/wb/bdj/controller/x", "g", "Ljavax/media/Player;")
    prefetch_method = find_cp_interface_methodref(entries, "javax/media/Controller", "prefetch", "()V")
    if not (player_field and prefetch_method):
        return data, {
            "label": "skip music jukebox startup prefetch",
            "matches": 0,
            "already_patched": False,
            "error": "required Warner music jukebox prefetch references were not found",
        }
    old = (
        b"\x2d\xb4" + player_field.to_bytes(2, "big")
        + b"\xb9" + prefetch_method.to_bytes(2, "big") + b"\x01\x00"
    )
    return replace_in_method_code_resized(
        data,
        method_name="a",
        descriptor="(Lcom/wb/bdj/controller/q;)V",
        old=old,
        new=b"\x00" * len(old),
        label="skip music jukebox startup prefetch",
        expected_matches=1,
    )


def patch_topmenu_activation_psr_branch(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    er_a_index = find_cp_methodref(entries, "er", "a", "()I")
    if not er_a_index:
        return data, {
            "label": "force top-menu activation when PSR4 is nonzero",
            "matches": 0,
            "already_patched": False,
            "error": "required er.a() method reference was not found",
        }
    old = b"\xb8" + er_a_index.to_bytes(2, "big") + b"\x9a\x00\x0d"
    new = b"\xb8" + er_a_index.to_bytes(2, "big") + b"\x57\x00\x00"
    return replace_class_sequence_count(
        data,
        old,
        new,
        "force top-menu activation when PSR4 is nonzero",
        expected_matches=1,
    )


def patch_topmenu_draw_psr_branch(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    er_a_index = find_cp_methodref(entries, "er", "a", "()I")
    hm_field_index = find_cp_fieldref(entries, None, "a", "Lhm;")
    e_method_index = find_cp_methodref(entries, "kl", "E", "(I)V")
    if not (er_a_index and hm_field_index and e_method_index):
        return data, {
            "label": "force top-menu draw when PSR4 is nonzero",
            "matches": 0,
            "already_patched": False,
            "error": "required top-menu draw method references were not found",
        }
    old = (
        b"\xb8" + er_a_index.to_bytes(2, "big")
        + b"\x99\x00\x0c"
        + b"\x2a\xb4" + hm_field_index.to_bytes(2, "big")
        + b"\x03\xb6" + e_method_index.to_bytes(2, "big")
        + b"\xb1"
    )
    new = (
        b"\xb8" + er_a_index.to_bytes(2, "big")
        + b"\x57"
        + b"\xa7\x00\x0b"
        + (b"\x00" * 8)
    )
    return replace_class_sequence_count(
        data,
        old,
        new,
        "force top-menu draw when PSR4 is nonzero",
        expected_matches=1,
    )


def patch_topmenu_remote_root_menu(data: bytes) -> tuple[bytes, dict[str, Any]]:
    entries, _ = parse_constant_pool(data)
    resume_field_index = find_cp_fieldref(entries, None, "u", "Lag;")
    resume_draw_index = find_cp_methodref(entries, "ag", "d_", "()V")
    root_menu_index = find_cp_methodref(entries, "iz", "H", "()Ldn;")
    menu_draw_index = find_cp_methodref(entries, "dn", "d_", "()V")
    if not (resume_field_index and resume_draw_index and root_menu_index and menu_draw_index):
        return data, {
            "label": "redraw root menu on disc-menu remote return",
            "matches": 0,
            "already_patched": False,
            "error": "required root-menu method references were not found",
        }
    old = b"\x2a\xb4" + resume_field_index.to_bytes(2, "big") + b"\xb6" + resume_draw_index.to_bytes(2, "big")
    new = b"\x2a\xb6" + root_menu_index.to_bytes(2, "big") + b"\xb6" + menu_draw_index.to_bytes(2, "big")
    return replace_in_method_code(
        data,
        method_name="j",
        descriptor="()V",
        old=old,
        new=new,
        label="redraw root menu on disc-menu remote return",
        expected_matches=1,
    )


def patch_topmenu_rebuild_scene_on_return(data: bytes) -> tuple[bytes, dict[str, Any]]:
    patched = data
    patched, jp_init_index = add_cp_methodref(patched, "jp", "a", "(Lcom/bydeluxe/bluray/msg/MessageQueue;)V")
    patched, jp_show_index = add_cp_methodref(patched, "jp", "b", "()V")
    patched, jp_refresh_index = add_cp_methodref(patched, "jp", "d", "()V")
    patched, jp_key_index = add_cp_methodref(patched, "jp", "a", "(Ljava/awt/event/KeyListener;)V")
    patched, gx_u_index = add_cp_methodref(patched, "gx", "U", "()V")
    patched, gx_t_index = add_cp_methodref(patched, "gx", "T", "()V")
    entries, _ = parse_constant_pool(patched)
    hm_field_index = find_cp_fieldref(entries, None, "r", "Lhm;")
    er_a_index = find_cp_methodref(entries, "er", "a", "()I")
    if not (hm_field_index and er_a_index):
        return data, {
            "label": "rebuild HScene before disc-menu remote redraw",
            "matches": 0,
            "already_patched": False,
            "error": "required scene rebuild references were not found",
        }
    marker = b"\xb8" + er_a_index.to_bytes(2, "big") + b"\x57\x00\x00"
    insertion = (
        b"\x2a\xb4" + hm_field_index.to_bytes(2, "big")
        + b"\xb8" + jp_init_index.to_bytes(2, "big")
        + b"\xb8" + jp_show_index.to_bytes(2, "big")
        + b"\xb8" + jp_refresh_index.to_bytes(2, "big")
        + b"\x2a\xb4" + hm_field_index.to_bytes(2, "big")
        + b"\xb8" + jp_key_index.to_bytes(2, "big")
        + b"\x2a\xb4" + hm_field_index.to_bytes(2, "big")
        + b"\xb6" + gx_u_index.to_bytes(2, "big")
        + b"\x2a\xb4" + hm_field_index.to_bytes(2, "big")
        + b"\xb6" + gx_t_index.to_bytes(2, "big")
    )
    return insert_in_method_code(
        patched,
        method_name="j",
        descriptor="()V",
        marker=marker,
        insertion=insertion,
        label="rebuild HScene before disc-menu remote redraw",
    )


def compatibility_fix_names_from_args(args: argparse.Namespace) -> list[str]:
    if getattr(args, "no_bdj_compatibility_patches", False):
        return []
    mode = getattr(args, "vlc_compat", DEFAULT_VLC_COMPATIBILITY_MODE)
    explicit = list(getattr(args, "vlc_fix", None) or [])
    if mode == "off":
        return canonical_vlc_fix_names(explicit)
    if explicit:
        return canonical_vlc_fix_names(explicit)
    return ["auto"]


def canonical_vlc_fix_names(fixes: list[str]) -> list[str]:
    canonical: list[str] = []
    for fix in fixes:
        name = VLC_COMPATIBILITY_FIX_ALIASES.get(fix, fix)
        if name not in canonical:
            canonical.append(name)
    return canonical


def custom_compatibility_patch_files_from_args(args: argparse.Namespace) -> list[Path]:
    return [Path(path).resolve() for path in (getattr(args, "compat_patch_file", None) or [])]


def apply_custom_class_operation(data: bytes, operation: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    op_type = operation.get("type")
    label = operation.get("label") or op_type or "custom operation"
    expected = operation.get("expected_matches", 1)
    if expected == "any":
        expected_matches = None
    else:
        expected_matches = int(expected) if expected is not None else None
    if op_type == "replace_hex":
        old = bytes.fromhex(str(operation["find"]))
        new = bytes.fromhex(str(operation["replace"]))
        return replace_class_sequence_count(data, old, new, label, expected_matches=expected_matches)
    if op_type == "replace_method_call":
        opcode_name = str(operation.get("opcode") or "invokevirtual")
        opcode = {"invokevirtual": 0xB6, "invokestatic": 0xB8, "invokeinterface": 0xB9}.get(opcode_name)
        if opcode is None:
            raise ToolError(f"Unsupported custom patch opcode: {opcode_name}")
        if opcode == 0xB9:
            raise ToolError("Custom invokeinterface replacement is not supported yet")
        return patch_class_methodref_call(
            data,
            opcode=opcode,
            from_class=str(operation["from_class"]),
            from_name=str(operation["from_name"]),
            from_descriptor=str(operation["from_descriptor"]),
            to_class=str(operation["to_class"]),
            to_name=str(operation["to_name"]),
            to_descriptor=str(operation["to_descriptor"]),
            label=label,
            expected_matches=expected_matches,
        )
    raise ToolError(f"Unsupported custom patch operation type: {op_type}")


def apply_custom_compatibility_patch_to_jar(jar_path: Path, spec: dict[str, Any], *, backup: bool = True) -> dict[str, Any]:
    jar_glob = str(spec.get("jar_glob") or "*.jar")
    if not jar_path.match(jar_glob) and not Path(jar_path.name).match(jar_glob):
        return {"jar": str(jar_path), "patch": spec.get("id"), "skipped": True, "reason": f"jar does not match {jar_glob}"}
    entry_name = str(spec["entry"])
    backup_path = jar_path.with_suffix(jar_path.suffix + ".bak_before_custom_compat_patch")
    temp_path = jar_path.with_suffix(jar_path.suffix + ".custom.tmp")
    report: dict[str, Any] = {
        "jar": str(jar_path),
        "patch": spec.get("id") or "custom",
        "entry": entry_name,
        "patched": False,
        "already_patched": False,
        "removed_signatures": [],
        "operations": [],
        "backup": str(backup_path) if backup else None,
    }
    if backup and not backup_path.exists():
        shutil.copy2(jar_path, backup_path)
    with zipfile.ZipFile(jar_path, "r") as zin, zipfile.ZipFile(temp_path, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            upper_name = info.filename.upper()
            if spec.get("remove_signatures", True) and upper_name.startswith("META-INF/") and upper_name.endswith((".SF", ".RSA", ".DSA", ".EC")):
                report["removed_signatures"].append(info.filename)
                report["patched"] = True
                continue
            if info.filename == entry_name:
                for operation in spec.get("operations") or []:
                    data, op_report = apply_custom_class_operation(data, operation)
                    report["operations"].append(op_report)
                    if op_report.get("error"):
                        temp_path.unlink(missing_ok=True)
                        raise ToolError(f"Custom patch failed for {jar_path}: {op_report}")
                    report["patched"] = report["patched"] or bool(op_report.get("matches"))
                    report["already_patched"] = report["already_patched"] or bool(op_report.get("already_patched"))
            zout.writestr(clone_zip_info(info), data)
    report["replace"] = replace_file_with_retry(temp_path, jar_path)
    return report


def load_custom_compatibility_patch_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(read_text_flexible(path))
    patches = payload.get("patches", payload if isinstance(payload, list) else None)
    if not isinstance(patches, list):
        raise ToolError(f"Custom compatibility patch file must contain a patches list: {path}")
    for patch in patches:
        patch.setdefault("source_file", str(path))
    return patches


def patch_bluray_vlc_menu_jar(jar_path: Path, *, fixes: list[str], backup: bool = True) -> dict[str, Any]:
    if not jar_path.exists():
        return {"jar": str(jar_path), "exists": False, "patched": False}
    backup_path = jar_path.with_suffix(jar_path.suffix + ".bak_before_codex_bdj_patch")
    temp_path = jar_path.with_suffix(jar_path.suffix + ".tmp")
    report: dict[str, Any] = {
        "jar": str(jar_path),
        "exists": True,
        "patched": False,
        "backup": str(backup_path) if backup else None,
        "removed_signatures": [],
        "fixes": fixes,
        "entries": [],
        "already_patched": False,
    }
    if backup and not backup_path.exists():
        shutil.copy2(jar_path, backup_path)
    with zipfile.ZipFile(jar_path, "r") as zin, zipfile.ZipFile(temp_path, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            upper_name = info.filename.upper()
            if upper_name.startswith("META-INF/") and upper_name.endswith((".SF", ".RSA", ".DSA", ".EC")):
                report["removed_signatures"].append(info.filename)
                report["patched"] = True
                continue
            if info.filename == "jp.class":
                entry_report: dict[str, Any] = {"entry": info.filename, "patches": []}
                if "hscene-menu-scene-show" in fixes:
                    data, show_patch = patch_jp_menu_start_show(data)
                    entry_report["patches"].append(show_patch)
                if "hscene-menu-preserve-scene-on-title" in fixes:
                    data, preserve_patch = patch_jp_preserve_scene_on_title(data)
                    entry_report["patches"].append(preserve_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "gx.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "hscene-menu-graphics-refresh" in fixes:
                    data, refresh_patch = patch_gx_menu_reacquire_graphics_after_show(data)
                    entry_report["patches"].append(refresh_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "disneyFramework/DisneyXlet$16.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "hscene-menu-remote-repaint" in fixes:
                    data, remote_patch = patch_menu_remote_show_repaint(data)
                    entry_report["patches"].append(remote_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "iz.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "hscene-menu-force-topmenu-activate" in fixes:
                    data, state_patch = patch_topmenu_activation_psr_branch(data)
                    entry_report["patches"].append(state_patch)
                if "hscene-menu-remote-root-menu" in fixes:
                    data, root_patch = patch_topmenu_remote_root_menu(data)
                    entry_report["patches"].append(root_patch)
                if "hscene-menu-rebuild-scene-on-return" in fixes:
                    data, rebuild_patch = patch_topmenu_rebuild_scene_on_return(data)
                    entry_report["patches"].append(rebuild_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "ag.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "hscene-menu-force-active-draw" in fixes:
                    data, draw_patch = patch_topmenu_draw_psr_branch(data)
                    entry_report["patches"].append(draw_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "dn.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "topmenu-mark-zero-on-return" in fixes:
                    data, mark_patch = patch_topmenu_mark_zero_on_return(data)
                    entry_report["patches"].append(mark_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "com/wb/bdj/menu/MusicJukeboxButtonHelper.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "music-jukebox-queued-state" in fixes:
                    data, jukebox_patch = patch_music_jukebox_button_queues_state(data)
                    entry_report["patches"].append(jukebox_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "com/wb/bdj/menu/be.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "music-jukebox-queued-state" in fixes:
                    data, group_patch = patch_music_jukebox_button_keeps_playlist_group_separate(data)
                    entry_report["patches"].append(group_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "com/wb/bdj/menu/k.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "music-jukebox-queued-state" in fixes:
                    data, position_patch = patch_music_jukebox_group_restores_authored_position(data)
                    entry_report["patches"].append(position_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "com/wb/bdj/controller/MusicJukeboxState.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "music-jukebox-queued-state" in fixes:
                    data, focus_patch = patch_music_jukebox_state_restores_default_focus(data)
                    entry_report["patches"].append(focus_patch)
                    data, state_component_patch = patch_music_jukebox_state_uses_current_menu_component(data)
                    entry_report["patches"].append(state_component_patch)
                    data, overlay_reset_patch = patch_music_jukebox_state_resets_overlay_before_redraw(data)
                    entry_report["patches"].append(overlay_reset_patch)
                    data, entry_redraw_patch = patch_music_jukebox_state_schedules_entry_redraw(data)
                    entry_report["patches"].append(entry_redraw_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            if info.filename == "com/wb/bdj/controller/b.class":
                entry_report = {"entry": info.filename, "patches": []}
                if "music-jukebox-queued-state" in fixes:
                    data, timer_patch = patch_music_jukebox_idle_timer_keeps_playlist_visible(data)
                    entry_report["patches"].append(timer_patch)
                    data, timer_component_patch = patch_music_jukebox_idle_timer_restores_graphics_lookup(data)
                    entry_report["patches"].append(timer_component_patch)
                    data, incoming_playlist_patch = patch_music_jukebox_idle_timer_draws_incoming_playlist(data)
                    entry_report["patches"].append(incoming_playlist_patch)
                    data, group_geometry_patch = patch_music_jukebox_idle_timer_restores_group_geometry(data)
                    entry_report["patches"].append(group_geometry_patch)
                    data, repeating_timer_patch = patch_music_jukebox_idle_timer_repeats_playlist_redraw(data)
                    entry_report["patches"].append(repeating_timer_patch)
                entry_report["patched"] = any(p.get("matches") == 1 for p in entry_report["patches"])
                entry_report["already_patched"] = any(p.get("already_patched") for p in entry_report["patches"])
                entry_report["ok"] = all("error" not in p for p in entry_report["patches"]) and (
                    not entry_report["patches"] or entry_report["patched"] or entry_report["already_patched"]
                )
                report["entries"].append(entry_report)
                if not entry_report["ok"]:
                    temp_path.unlink(missing_ok=True)
                    raise ToolError(f"Could not safely patch {jar_path}: {entry_report}")
                report["patched"] = report["patched"] or entry_report["patched"]
                report["already_patched"] = report["already_patched"] or entry_report["already_patched"]
            zout.writestr(clone_zip_info(info), data)
    report["replace"] = replace_file_with_retry(temp_path, jar_path)
    return report


def parse_simple_properties(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def music_jukebox_menu_layers(props: dict[str, str]) -> list[dict[str, str]]:
    layers: list[dict[str, str]] = []
    for key, value in props.items():
        if not key.endswith(".class"):
            continue
        if not value.startswith("com.wb.bdj.menu.MusicJukeboxButtonHelper,"):
            continue
        button_id = key[:-6]
        popup_id = props.get(f"{button_id}.jukeboxMenuId")
        group_id = props.get(f"{button_id}.playlistMenuId")
        if not popup_id or not group_id:
            continue
        if props.get(f"{popup_id}.type") != "Menu" or props.get(f"{group_id}.type") != "RadioGroup":
            continue
        children = props.get(f"{popup_id}.children")
        group_children = props.get(f"{group_id}.children")
        if not children or not group_children:
            continue
        layers.append(
            {
                "button": button_id,
                "popup": popup_id,
                "group": group_id,
                "children": children,
            }
        )
    return layers


def patch_music_jukebox_menu_layer_properties(prop_path: Path, *, backup: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {
        "file": str(prop_path),
        "exists": prop_path.exists(),
        "patched": False,
        "already_patched": False,
        "layers": [],
    }
    if not prop_path.exists():
        return report
    text = prop_path.read_bytes().decode("ISO-8859-1")
    props = parse_simple_properties(text)
    layers = music_jukebox_menu_layers(props)
    if not layers:
        report["skipped"] = True
        report["reason"] = "no matching Warner music jukebox menu layer"
        return report

    updated = text
    matched_layers: list[dict[str, Any]] = []
    for layer in layers:
        popup_id = layer["popup"]
        group_id = layer["group"]
        children = layer["children"]
        child_ids = [item.strip() for item in children.split(",") if item.strip()]
        layer_report: dict[str, Any] = {
            "button": layer["button"],
            "popup": popup_id,
            "group": group_id,
            "already_patched": group_id not in child_ids,
            "patched": False,
        }
        if group_id in child_ids:
            old_line = f"{popup_id}.children={children}"
            new_children = ",".join(child_id for child_id in child_ids if child_id != group_id)
            new_line = f"{popup_id}.children={new_children}"
            if old_line not in updated:
                layer_report["error"] = f"children line not found for {popup_id}"
            else:
                updated = updated.replace(old_line, new_line, 1)
                layer_report["patched"] = True
        matched_layers.append(layer_report)

    errors = [layer for layer in matched_layers if layer.get("error")]
    report["layers"] = matched_layers
    if errors:
        report["error"] = "; ".join(str(layer["error"]) for layer in errors)
        return report
    if updated != text:
        backup_path = prop_path.with_suffix(prop_path.suffix + ".bak_before_codex_bdj_patch")
        report["backup"] = str(backup_path) if backup else None
        if backup and not backup_path.exists():
            shutil.copy2(prop_path, backup_path)
        prop_path.write_bytes(updated.encode("ISO-8859-1"))
        report["patched"] = True
    report["already_patched"] = bool(matched_layers) and all(layer.get("already_patched") for layer in matched_layers)
    return report


def patch_music_jukebox_menu_layers(root: Path, *, backup: bool = True) -> dict[str, Any]:
    jar_dir = root / "BDMV" / "JAR"
    prop_files = sorted(jar_dir.glob("*/menu_base.prop")) if jar_dir.is_dir() else []
    reports = [patch_music_jukebox_menu_layer_properties(path, backup=backup) for path in prop_files]
    matched = [item for item in reports if item.get("layers")]
    return {
        "target": str(root),
        "patch": "music-jukebox-menu-layer",
        "files": reports,
        "patched": any(item.get("patched") for item in reports),
        "already_patched": bool(matched) and all(item.get("already_patched") for item in matched),
    }


def patch_bluray_vlc_menu(target: Path, *, fixes: list[str] | None = None, backup: bool = True) -> dict[str, Any]:
    roots = find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    root = roots[0]
    jar_dirs = [root / "BDMV" / "JAR", root / "BDMV" / "BACKUP" / "JAR"]
    jars = [jar for jar_dir in jar_dirs if jar_dir.is_dir() for jar in sorted(jar_dir.glob("*.jar"))]
    if not jars:
        raise ToolError(f"No BD-J JAR files found under {root}")
    selected_fixes = canonical_vlc_fix_names(
        fixes
        or [
            "music-jukebox-queued-state",
            "topmenu-mark-zero-on-return",
        ]
    )
    from .wb_jukebox import matches_disc as matches_stable_jukebox, patch_disc as patch_stable_jukebox
    stable_jukebox = None
    jar_fixes = selected_fixes
    if "music-jukebox-queued-state" in selected_fixes and matches_stable_jukebox(root):
        stable_jukebox = patch_stable_jukebox(root, backup=backup)
        jar_fixes = [fix for fix in selected_fixes if fix != "music-jukebox-queued-state"]
    reports = [patch_bluray_vlc_menu_jar(jar, fixes=jar_fixes, backup=backup) for jar in jars] if jar_fixes else []
    resource_report = None
    if stable_jukebox:
        resource_report = stable_jukebox
    elif "music-jukebox-queued-state" in selected_fixes:
        resource_report = patch_music_jukebox_menu_layers(root, backup=backup)
    return {
        "target": str(root),
        "patch": "bluray-vlc-menu",
        "fixes": selected_fixes,
        "warning": "experimental VLC/libbluray compatibility patch; backs up original BD-J files before changing them",
        "jars": reports,
        "resources": resource_report,
        "patched": any(item.get("patched") for item in reports) or bool(resource_report and resource_report.get("patched")),
        "already_patched": all(item.get("already_patched") for item in reports if item.get("entries"))
        and (not resource_report or resource_report.get("already_patched") or not resource_report.get("files")),
    }


def jar_entry_contains(jar_path: Path, entry_name: str, needle: bytes) -> bool:
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                return needle in zf.read(entry_name)
            except KeyError:
                return False
    except zipfile.BadZipFile:
        return False


def jar_has_hscene_menu_lifecycle(jar_path: Path) -> bool:
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                data = zf.read("gx.class")
            except KeyError:
                return False
    except zipfile.BadZipFile:
        return False
    try:
        entries, _ = parse_constant_pool(data)
    except Exception:
        return False
    return bool(find_cp_methodref(entries, "jp", "b", "()V") and find_cp_methodref(entries, "jp", "d", "()V"))


def jar_has_menu_remote_topmenu(jar_path: Path) -> bool:
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                data = zf.read("disneyFramework/DisneyXlet$16.class")
            except KeyError:
                return False
    except zipfile.BadZipFile:
        return False
    try:
        entries, _ = parse_constant_pool(data)
    except Exception:
        return False
    return bool(find_cp_methodref(entries, "hm", "bV", "()V") and find_cp_methodref(entries, "java/awt/Component", "requestFocus", "()V"))


def jar_has_topmenu_mark_zero_signature(jar_path: Path) -> bool:
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                data = zf.read("dn.class")
            except KeyError:
                return False
    except zipfile.BadZipFile:
        return False
    try:
        entries, _ = parse_constant_pool(data)
    except Exception:
        return False
    return bool(
        find_cp_fieldref(entries, "dn", "b", "Laq;")
        and find_cp_fieldref(entries, "dn", "j", "I")
        and find_cp_methodref(entries, "aq", "b", "()B")
        and find_cp_methodref(entries, "dn", "P", "()V")
        and find_cp_methodref(entries, "ae", "f", "()I")
        and find_cp_methodref(entries, "bd", "a", "(IILlb;)V")
    )


def jar_has_music_jukebox_queued_state_signature(jar_path: Path) -> bool:
    try:
        with zipfile.ZipFile(jar_path, "r") as zf:
            try:
                helper_data = zf.read("com/wb/bdj/menu/MusicJukeboxButtonHelper.class")
                menu_data = zf.read("com/wb/bdj/menu/be.class")
                group_data = zf.read("com/wb/bdj/menu/k.class")
                state_data = zf.read("com/wb/bdj/controller/MusicJukeboxState.class")
                timer_data = zf.read("com/wb/bdj/controller/b.class")
            except KeyError:
                return False
    except zipfile.BadZipFile:
        return False
    patch_checks = (
        (helper_data, patch_music_jukebox_button_queues_state),
        (menu_data, patch_music_jukebox_button_keeps_playlist_group_separate),
        (group_data, patch_music_jukebox_group_restores_authored_position),
        (state_data, patch_music_jukebox_state_restores_default_focus),
        (state_data, patch_music_jukebox_state_uses_current_menu_component),
        (state_data, patch_music_jukebox_state_resets_overlay_before_redraw),
        (state_data, patch_music_jukebox_state_schedules_entry_redraw),
        (timer_data, patch_music_jukebox_idle_timer_keeps_playlist_visible),
        (timer_data, patch_music_jukebox_idle_timer_restores_graphics_lookup),
        (timer_data, patch_music_jukebox_idle_timer_draws_incoming_playlist),
        (timer_data, patch_music_jukebox_idle_timer_restores_group_geometry),
        (timer_data, patch_music_jukebox_idle_timer_repeats_playlist_redraw),
    )
    for data, patcher in patch_checks:
        try:
            _, report = patcher(data)
        except Exception:
            return False
        if report.get("error") or not (report.get("matches") == 1 or report.get("already_patched")):
            return False
    return True


def should_apply_hscene_menu_vlc_patch(root: Path) -> bool:
    jar_dirs = [root / "BDMV" / "JAR", root / "BDMV" / "BACKUP" / "JAR"]
    for jar_dir in jar_dirs:
        if not jar_dir.is_dir():
            continue
        for jar_path in jar_dir.glob("*.jar"):
            if jar_has_topmenu_mark_zero_signature(jar_path):
                return True
    return False


def should_apply_music_jukebox_vlc_patch(root: Path) -> bool:
    from .wb_jukebox import matches_disc
    if matches_disc(root):
        return True
    jar_dirs = [root / "BDMV" / "JAR", root / "BDMV" / "BACKUP" / "JAR"]
    for jar_dir in jar_dirs:
        if not jar_dir.is_dir():
            continue
        for jar_path in jar_dir.glob("*.jar"):
            if jar_has_music_jukebox_queued_state_signature(jar_path):
                return True
    return False


def patch_known_bdj_compatibility(
    target: Path,
    *,
    fixes: list[str] | None = None,
    custom_patch_files: list[Path] | None = None,
) -> dict[str, Any]:
    roots = find_disc_roots([target])
    if not roots:
        raise ToolError(f"No BDMV folder found at {target}")
    root = roots[0]
    patches: list[dict[str, Any]] = []
    requested = ["auto"] if fixes is None else canonical_vlc_fix_names(list(fixes))
    known_requested = [
        "music-jukebox-queued-state",
        "topmenu-mark-zero-on-return",
    ] if "auto" in requested else requested
    unknown = [name for name in known_requested if name not in KNOWN_VLC_COMPATIBILITY_FIXES]
    if unknown:
        raise ToolError(f"Unknown VLC compatibility fix: {', '.join(unknown)}")
    applicable_known: list[str] = []
    if "music-jukebox-queued-state" in known_requested and should_apply_music_jukebox_vlc_patch(root):
        applicable_known.append("music-jukebox-queued-state")
    if "topmenu-mark-zero-on-return" in known_requested and should_apply_hscene_menu_vlc_patch(root):
        applicable_known.append("topmenu-mark-zero-on-return")
    if applicable_known:
        patches.append(patch_bluray_vlc_menu(root, fixes=applicable_known, backup=True))
    for patch_file in custom_patch_files or []:
        custom_specs = load_custom_compatibility_patch_file(patch_file)
        jar_dirs = [root / "BDMV" / "JAR", root / "BDMV" / "BACKUP" / "JAR"]
        jars = [jar for jar_dir in jar_dirs if jar_dir.is_dir() for jar in sorted(jar_dir.glob("*.jar"))]
        for spec in custom_specs:
            reports = [apply_custom_compatibility_patch_to_jar(jar, spec, backup=True) for jar in jars]
            patches.append(
                {
                    "target": str(root),
                    "patch": spec.get("id") or "custom",
                    "source_file": spec.get("source_file"),
                    "jars": reports,
                    "patched": any(item.get("patched") for item in reports),
                    "already_patched": all(item.get("already_patched") for item in reports if not item.get("skipped")),
                }
            )
    return {
        "target": str(root),
        "patches": patches,
        "patched": any(patch.get("patched") for patch in patches),
    }
