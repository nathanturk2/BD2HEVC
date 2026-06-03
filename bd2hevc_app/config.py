"""Shared configuration constants for BD2HEVC."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"

LOCAL_TSMUXERS = [
    ROOT / "tools" / "tsmuxer-2.7.0" / "tsMuxeR.exe",
    ROOT / "tools" / "tsmuxer-2.7.0" / "tsmuxer",
    ROOT / "tools" / "tsmuxer-2.7.0" / "tsMuxeR",
    ROOT / "tools" / "tsmuxer" / "tsmuxer.exe",
    ROOT / "tools" / "tsmuxer" / "tsmuxer",
    ROOT / "tools" / "tsmuxer" / "tsMuxeR",
]
LOCAL_FFMPEG_DIRS = [
    ROOT / "tools" / "ffmpeg" / "bin",
    ROOT / "tools" / "ffmpeg",
]
MAKEMKV_DIRS = [
    Path(r"C:\Program Files (x86)\MakeMKV"),
    Path(r"C:\Program Files\MakeMKV"),
]
VLC_DIRS = [
    Path(r"C:\Program Files\VideoLAN\VLC"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC"),
]

DEFAULT_REPORT_DIR = ROOT / "reports"
DEFAULT_JOB_DIR = DEFAULT_REPORT_DIR / "jobs"
QUEUE_PAUSE_FILE = DEFAULT_JOB_DIR / "queue.paused"

SECONDS_REENCODE_THRESHOLD = 10.0
MPEG2_SOURCE_CODECS = {"mpeg1video", "mpeg2video"}
DEFAULT_MAKEMKV_TIMEOUT_SECONDS = 300.0
QUEUE_POLL_SECONDS = 10.0

ANIME_CQ_PRESET = "compact-cq"
LEGACY_EPISODE_COMPACT_PRESET = "episode-compact"
LEGACY_ANIME_CQ_PRESET = "anime-cq18"
BITRATE_MODE_ALIASES = {
    LEGACY_EPISODE_COMPACT_PRESET: ANIME_CQ_PRESET,
    LEGACY_ANIME_CQ_PRESET: ANIME_CQ_PRESET,
}
DEFAULT_COMPACT_CQ_VALUE = 18
ANIME_CQ_VALUE = DEFAULT_COMPACT_CQ_VALUE
DEFAULT_ANIME_CQ_MIN_DURATION = SECONDS_REENCODE_THRESHOLD
BITRATE_MODES = ("smaller", "balanced", "transparent", "source-ratio", ANIME_CQ_PRESET, LEGACY_EPISODE_COMPACT_PRESET, LEGACY_ANIME_CQ_PRESET)

HEVC_ENCODERS = ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265")
HARDWARE_HEVC_ENCODERS = {"hevc_nvenc", "hevc_qsv", "hevc_amf"}

DEINTERLACE_MODES = ("off", "auto", "force")
DEINTERLACE_FILTERS = ("bwdif", "yadif")
INTERLACED_FIELD_ORDERS = {
    "tt",
    "bb",
    "tb",
    "bt",
    "top coded first (swapped)",
    "bottom coded first (swapped)",
    "top coded first",
    "bottom coded first",
}

AUDIO_MODES = ("passthrough", "compact-stereo")
DEFAULT_AUDIO_MODE = "passthrough"
DEFAULT_STEREO_AUDIO_BITRATE = 256_000
DEFAULT_MONO_AUDIO_BITRATE = 128_000

SPARSE_TIMING_FRAME_COUNT_MAX_DURATION = 900.0
SPARSE_TIMING_ALWAYS_COUNT_MAX_DURATION = 60.0
SPARSE_TIMING_MIN_GAP_SECONDS = 2.0
SPARSE_TIMING_MIN_RATIO = 1.5

CLPI_PRIMARY_VIDEO_AVC = bytes.fromhex("001011151b61")
CLPI_PRIMARY_VIDEO_HEVC = bytes.fromhex("001011152461")
CLPI_PRIMARY_VIDEO_MPEG2 = bytes.fromhex("001011150261")
MPLS_PRIMARY_VIDEO_AVC = bytes.fromhex("09011011000000000000051b")
MPLS_PRIMARY_VIDEO_HEVC = bytes.fromhex("090110110000000000000524")
MPLS_PRIMARY_VIDEO_MPEG2 = bytes.fromhex("090110110000000000000502")

HSCENE_MENU_START_SET_VISIBLE = bytes.fromhex("b2002704b60058")
DEFAULT_VLC_COMPATIBILITY_MODE = "auto"
KNOWN_VLC_COMPATIBILITY_FIXES = {
    "topmenu-mark-zero-on-return": "Normalize matching BD-J top-menu playlist returns to start at the menu entry point instead of a stale positive playmark.",
}
VLC_COMPATIBILITY_FIX_ALIASES = {}
