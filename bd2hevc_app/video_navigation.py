"""Parse video descriptors and reconcile them with measured output streams."""
from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path

from .tools import ToolError, require_tool, run_cmd

VIDEO_CODECS = {"mpeg1video": 1, "mpeg2video": 2, "h264": 0x1B, "hevc": 0x24, "vc1": 0xEA}
VIDEO_TYPES = set(VIDEO_CODECS.values()) | {0x20}
RATES = {Fraction(24000, 1001): 1, Fraction(24): 2, Fraction(25): 3,
         Fraction(30000, 1001): 4, Fraction(50): 6, Fraction(60000, 1001): 7}


def be(data, offset, length):
    return int.from_bytes(data[offset:offset + length], "big")


def clpi_video_entries(data: bytes | bytearray):
    if len(data) < 28 or data[:4] != b"HDMV":
        raise ToolError("Not a CLPI file")
    start = be(data, 12, 4)
    end = start + 4 + be(data, start, 4)
    if start < 28 or start + 6 > end or end > len(data):
        raise ToolError("Invalid CLPI program information")
    pos = start + 6
    for _ in range(data[start + 5]):
        if pos + 8 > end:
            raise ToolError("Truncated CLPI program")
        count = data[pos + 6]
        pos += 8
        for _ in range(count):
            if pos + 3 > end:
                raise ToolError("Truncated CLPI stream")
            pid, length = be(data, pos, 2), data[pos + 2]
            attr = pos + 3
            if not length or attr + length > end:
                raise ToolError("Invalid CLPI stream length")
            if data[attr] in VIDEO_TYPES:
                yield pid, attr, length
            pos = attr + length


def mpls_video_entries(data: bytes | bytearray):
    if len(data) < 20 or data[:4] != b"MPLS":
        raise ToolError("Not an MPLS file")
    start = be(data, 8, 4)
    end = start + 4 + be(data, start, 4)
    if start < 20 or start + 10 > end or end > len(data):
        raise ToolError("Invalid playlist bounds")
    pos = start + 10
    for _ in range(be(data, start + 6, 2)):
        item_end = pos + 2 + be(data, pos, 2)
        if pos + 34 > item_end or item_end > end:
            raise ToolError("Invalid playitem bounds")
        clip = bytes(data[pos + 2:pos + 7]).decode("ascii")
        stn = pos + 34
        if data[pos + 12] & 0x10:
            if stn + 2 > item_end or data[stn] < 1:
                raise ToolError("Invalid playlist angle table")
            stn += 2 + (data[stn] - 1) * 10
        if stn + 16 > item_end:
            raise ToolError("Truncated stream-number table")
        stn_end = stn + 2 + be(data, stn, 2)
        if stn_end > item_end or stn_end < stn + 16:
            raise ToolError("Invalid stream-number table bounds")
        cursor = stn + 16
        for _ in range(data[stn + 4]):
            if cursor + 2 > stn_end:
                raise ToolError("Truncated video stream entry")
            length = data[cursor]
            entry = cursor + 1
            attr_length_pos = entry + length
            if length < 3 or attr_length_pos >= stn_end:
                raise ToolError("Invalid video stream entry")
            kind = data[entry]
            pid_offset = {1: 1, 2: 3, 3: 2, 4: 2}.get(kind)
            if pid_offset is None or pid_offset + 2 > length:
                raise ToolError("Unsupported video stream addressing")
            # Out-of-mux entries address another clip; do not apply this
            # playitem's in-mux stream properties to those entries.
            pid = be(data, entry + pid_offset, 2) if kind == 1 else None
            attr_length = data[attr_length_pos]
            attr = attr_length_pos + 1
            if attr_length < 2 or attr + attr_length > stn_end:
                raise ToolError("Invalid video stream attributes")
            yield clip, pid, attr, attr_length
            cursor = attr + attr_length
        pos = item_end


def stream_descriptor(stream: dict, *, assume_bd_sdr: bool = False) -> dict:
    codec = VIDEO_CODECS.get(stream.get("codec_name"))
    if codec is None:
        raise ToolError(f"Unsupported video codec: {stream.get('codec_name')}")
    raw_pid = stream.get("id")
    pid = int(raw_pid, 0) if isinstance(raw_pid, str) else raw_pid
    if pid is None:
        raise ToolError("Video PID missing from stream probe")
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    interlaced = stream.get("field_order") in ("tt", "bb", "tb", "bt")
    formats = {(1920, 1080): 4 if interlaced else 6, (1440, 1080): 4 if interlaced else 6,
               (3840, 2160): 8, (1280, 720): 5,
               (720, 480): 1 if interlaced else 3, (720, 576): 2 if interlaced else 7}
    fmt = formats.get((width, height))
    if fmt is None:
        raise ToolError(f"Cannot describe {width}x{height} in Blu-ray navigation")
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    if value == "0/0":
        value = stream.get("r_frame_rate") or "0/1"
    try:
        fps = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ToolError(f"Invalid frame rate: {value}") from None
    nearest = min(RATES, key=lambda rate: abs(float(rate - fps)))
    if abs(float(nearest - fps)) > 0.01:
        # Legacy still-picture streams can advertise a doubled probing rate;
        # preserve their existing rate unless a real encode supplied one.
        rate = None
    else:
        rate = RATES[nearest]
    primaries = stream.get("color_primaries")
    transfer = stream.get("color_transfer")
    color = {"bt709": 1, "bt2020": 2}.get(primaries)
    dynamic = 1 if transfer == "smpte2084" else (0 if transfer in ("bt709", "bt2020-10", "bt2020-12") else None)
    inferred = False
    if (assume_bd_sdr and primaries in (None, "unknown", "unspecified")
            and transfer in (None, "unknown", "unspecified", "bt709")
            and height >= 720 and stream.get("pix_fmt") in ("yuv420p", "nv12")):
        color, dynamic, inferred = 1, 0, True
    return {"pid": pid, "codec": codec, "format": fmt, "rate": rate,
            "color_space": color, "dynamic_range": dynamic, "inferred_bd709": inferred}


def probe_video(path: Path, tools: dict, *, assume_bd_sdr: bool = False, hevc_only: bool = False) -> dict[int, dict]:
    result = run_cmd([require_tool(tools, "ffprobe"), "-v", "error", "-probesize", "4000000",
                      "-analyzeduration", "2000000", "-select_streams", "v", "-show_entries",
                      "stream=id,codec_name,width,height,pix_fmt,field_order,r_frame_rate,avg_frame_rate,color_primaries,color_transfer",
                      "-of", "json", str(path)], timeout_seconds=45)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ToolError(f"No video streams found in {path}")
    if hevc_only:
        streams = [stream for stream in streams if stream.get("codec_name") == "hevc"]
    return {d["pid"]: d for d in (stream_descriptor(s, assume_bd_sdr=assume_bd_sdr) for s in streams)}


def _update(data: bytearray, offset: int, length: int, props: dict, *, clpi: bool) -> bool:
    before = bytes(data[offset:offset + length])
    hevc = props["codec"] == 0x24
    required = (5 if clpi else 4) if hevc else 2
    if length < required:
        raise ToolError("Video attributes are too short for a size-preserving metadata repair")
    data[offset] = props["codec"]
    if props.get("format") is not None:
        data[offset + 1] = (props["format"] << 4) | (data[offset + 1] & 15)
    if props.get("rate") is not None:
        data[offset + 1] = (data[offset + 1] & 0xF0) | props["rate"]
    if hevc:
        extra = offset + (3 if clpi else 2)
        if props.get("dynamic_range") is not None:
            data[extra] = (props["dynamic_range"] << 4) | (data[extra] & 15)
        if props.get("color_space") is not None:
            data[extra] = (data[extra] & 0xF0) | props["color_space"]
    return before != bytes(data[offset:offset + length])


def reconcile_clpi(data: bytearray, video: dict[int, dict]) -> int:
    entries = list(clpi_video_entries(data))
    return sum(_update(data, offset, length, video[pid], clpi=True)
               for pid, offset, length in entries if pid in video)


def reconcile_mpls(data: bytearray, clips: dict[str, dict[int, dict]]) -> int:
    entries = list(mpls_video_entries(data))
    return sum(_update(data, offset, length, clips[clip][pid], clpi=False)
               for clip, pid, offset, length in entries if pid in clips.get(clip, {}))
