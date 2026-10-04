"""Independent, bounded checks of UDF volume descriptors.

No writer-specific reader is used. Repairs are returned as byte patches; this
module never modifies an image. Both main and reserve descriptor sequences are
checked, including defects that permissive readers can silently accept.
"""
from __future__ import annotations

import binascii
from pathlib import Path

BLOCK = 2048
OSTA_CHARSET = b"\0OSTA Compressed Unicode".ljust(64, b"\0")


class UdfValidationError(ValueError):
    pass


def number(data: bytes, offset: int, size: int = 4) -> int:
    return int.from_bytes(data[offset:offset + size], "little")


def checked_tag(data: bytes, location: int | None = None) -> int:
    if len(data) < 16:
        raise UdfValidationError("Truncated UDF descriptor")
    length = number(data, 10, 2)
    if length > len(data) - 16:
        raise UdfValidationError("UDF descriptor CRC extends beyond its block")
    if data[4] != (sum(data[:4]) + sum(data[5:16])) & 255:
        raise UdfValidationError("UDF descriptor tag checksum mismatch")
    if binascii.crc_hqx(data[16:16 + length], 0) != number(data, 8, 2):
        raise UdfValidationError("UDF descriptor body CRC mismatch")
    if location is not None and number(data, 12) != location:
        raise UdfValidationError("UDF descriptor points to the wrong block")
    return number(data, 0, 2)


def update_tag(data: bytearray, length: int | None = None) -> None:
    length = number(data, 10, 2) if length is None else length
    data[10:12] = length.to_bytes(2, "little")
    data[8:10] = binascii.crc_hqx(data[16:16 + length], 0).to_bytes(2, "little")
    data[4] = 0
    data[4] = sum(data[:16]) & 255


def inspect_udf(path: Path) -> dict:
    """Return independently checked descriptors and narrowly repairable defects."""
    size = path.stat().st_size
    descriptors, patches, issues = [], [], []
    with path.open("rb") as handle:
        def read_block(block: int) -> bytes:
            if block < 0 or (block + 1) * BLOCK > size:
                raise UdfValidationError("UDF block outside image")
            handle.seek(block * BLOCK)
            data = handle.read(BLOCK)
            if len(data) != BLOCK:
                raise UdfValidationError("Short UDF block read")
            return data

        anchor = read_block(256)
        if checked_tag(anchor, 256) != 2:
            raise UdfValidationError("No UDF anchor at sector 256")
        sequences = [(number(anchor, 20), number(anchor, 16)),
                     (number(anchor, 28), number(anchor, 24))]
        if not all(length and length <= 1024 * BLOCK and length % BLOCK == 0 for _, length in sequences):
            raise UdfValidationError("Invalid UDF descriptor sequence extent")
        for start, length in sequences:
            found_lvd = found_end = False
            hadris = False
            for block in range(start, start + length // BLOCK):
                original = read_block(block)
                tag = checked_tag(original, block)
                hadris |= b"*hadris-udf" in original
                data = bytearray(original)
                reasons = []
                if tag == 6:
                    found_lvd = True
                    if number(data, 212) != BLOCK:
                        raise UdfValidationError("Unsupported UDF logical block size")
                    if data[20:84] != OSTA_CHARSET:
                        issues.append(f"Sector {block}: invalid logical-volume character set")
                        if hadris and not any(data[20:84]):
                            data[20:84] = OSTA_CHARSET
                            reasons.append("restore OSTA character set")
                if tag == 8:
                    found_end = True
                    if number(data, 10, 2) != 496:
                        issues.append(f"Sector {block}: incomplete terminating descriptor")
                        if hadris and number(data, 10, 2) == 0 and not any(data[16:512]):
                            update_tag(data, 496)
                            reasons.append("cover reserved terminating-descriptor body in CRC")
                if reasons:
                    update_tag(data)
                    patches.append({"offset": block * BLOCK, "before": original[:512].hex(),
                                    "after": bytes(data[:512]).hex(), "reason": "; ".join(reasons)})
                descriptors.append({"sector": block, "tag": tag, "data": original.hex()})
                if tag == 8:
                    break
            if not found_lvd or not found_end:
                raise UdfValidationError("UDF descriptor sequence is incomplete")
    return {"ok": not issues, "issues": issues, "patches": patches, "descriptors": descriptors}


def validate_udf(path: Path) -> dict:
    result = inspect_udf(path)
    if not result["ok"]:
        raise UdfValidationError("; ".join(result["issues"]))
    return {"ok": True, "descriptor_count": len(result["descriptors"])}
