"""Small structurally valid navigation files for descriptor regression tests."""


def clpi(codec=0x1B, format_rate=0x61, pid=0x1011):
    attributes = bytes([codec, format_rate, 0x30, 0, 0]) + bytes(16)
    program = bytes([0, 1]) + bytes(4) + b"\x01\x00\x01\x00"
    program += pid.to_bytes(2, "big") + bytes([len(attributes)]) + attributes
    header = bytearray(b"HDMV0200" + bytes(24))
    header[12:16] = (32).to_bytes(4, "big")
    return bytes(header) + len(program).to_bytes(4, "big") + program


def mpls(codec=0x1B, format_rate=0x61, clip="00001", pid=0x1011, kind=1):
    address = bytes([kind]) + (b"\0\0" if kind == 2 else b"") + pid.to_bytes(2, "big")
    address = address.ljust(9, b"\0")
    attributes = bytes([codec, format_rate, 0, 0, 0])
    stream = bytes([len(address)]) + address + bytes([len(attributes)]) + attributes
    table = bytes([0, 0, 1]) + bytes(11) + stream
    stn = len(table).to_bytes(2, "big") + table
    body = clip.encode() + b"M2TS" + bytes(23) + stn
    item = len(body).to_bytes(2, "big") + body
    playlist = bytes(2) + b"\0\x01" + bytes(2) + item
    return b"MPLS0200" + (20).to_bytes(4, "big") + bytes(8) + len(playlist).to_bytes(4, "big") + playlist
