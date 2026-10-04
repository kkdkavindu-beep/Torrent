"""Paths, constants and defaults for the Magnetar Colab pipeline.

Everything is derived from one base directory so the same code runs on
Google Colab (base = /content) and locally for testing (base = <repo>/workspace,
override with the MAGNETAR_BASE environment variable).
"""
from __future__ import annotations

import os
import pathlib
import shutil

IS_COLAB = os.path.isdir("/content")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _default_base() -> pathlib.Path:
    env = os.environ.get("MAGNETAR_BASE")
    if env:
        return pathlib.Path(env)
    if IS_COLAB:
        return pathlib.Path("/content")
    return REPO_ROOT / "workspace"


BASE_DIR = _default_base()
DOWNLOAD_DIR = BASE_DIR / "downloads"
CONVERTED_DIR = BASE_DIR / "converted"
TORRENT_STATE_DIR = BASE_DIR / "torrents"  # session resume data (Colab lifetime only)

DRIVE_MOUNT = pathlib.Path("/content/drive")
DRIVE_MYDRIVE = DRIVE_MOUNT / "MyDrive"
DEFAULT_DRIVE_FOLDER = "TorrentColab"


def drive_mounted() -> bool:
    return DRIVE_MYDRIVE.is_dir()


def drive_base(folder: str = DEFAULT_DRIVE_FOLDER) -> pathlib.Path:
    """Root directory for uploads on Google Drive (or a local staging dir off-Colab)."""
    folder = (folder or DEFAULT_DRIVE_FOLDER).strip().strip("/\\") or DEFAULT_DRIVE_FOLDER
    if drive_mounted():
        path = DRIVE_MYDRIVE / folder
    else:
        path = BASE_DIR / "drive_staging" / folder
    path.mkdir(parents=True, exist_ok=True)
    return path


def ffmpeg_cache_dir() -> pathlib.Path:
    """Where the NVENC-capable static ffmpeg is cached.

    On Colab this lives on Drive so the ~100 MB build survives session restarts.
    """
    env = os.environ.get("MAGNETAR_FFMPEG_CACHE")
    if env:
        path = pathlib.Path(env)
    elif IS_COLAB and drive_mounted():
        path = DRIVE_MYDRIVE / ".cache" / "magnetar_ffmpeg"
    else:
        path = BASE_DIR / ".cache" / "ffmpeg"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- file classes
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".wmv",
    ".flv", ".mpg", ".mpeg", ".m4v", ".3gp", ".ogv", ".vob", ".rm", ".rmvb",
    ".asf", ".divx", ".f4v",
}
AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma", ".mka",
    ".alac", ".ac3", ".dts",
}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}

# ------------------------------------------------------------ conversion presets
# label -> target pixel height
TARGET_HEIGHTS = {
    "240p": 240,
    "360p": 360,
    "480p": 480,
    "576p": 576,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,  # effectively "never downscale"
}
DEFAULT_TARGET = "720p"

CODEC_TARGETS = {"H.264 (fast, compatible)": "h264", "H.265/HEVC (smaller, slower)": "hevc"}
AUDIO_MODES = {"Copy audio": "copy", "Convert to AAC": "aac"}
CONTAINER_MODES = {"Keep original": "keep", "MP4": "mp4", "MKV": "mkv"}

# Resolution buckets by pixel height (portrait videos use their width instead).
RES_BUCKETS = [
    ("2160p", 2000),
    ("1440p", 1300),
    ("1080p", 900),
    ("720p", 700),
    ("576p", 560),
    ("480p", 450),
    ("360p", 340),
    ("240p", 200),
]

# Audio codecs that common players refuse inside an MP4 container -> force AAC
# when transcoding into mp4. MKV accepts everything, so it is never forced.
MP4_INCOMPATIBLE_AUDIO = ("dts", "truehd", "mlp", "pcm_", "eac3")

# ------------------------------------------------------------------- tuning
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "wss://tracker.openwebtorrent.com",
]

PROBE_TIMEOUT_SEC = 45.0      # magnet -> metadata wait (same as Magnetar)
PROBE_POLL_SEC = 0.4          # metadata poll interval
DOWNLOAD_POLL_SEC = 1.0       # progress poll interval
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_CONVERTS = 2   # T4 has a single NVENC chip; 2 pipelines overlap I/O nicely
AUDIO_BITRATE = "192k"

LOG = "magnetar_colab"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TB"


def resolution_label(width: int, height: int) -> str:
    if height <= 0 or width <= 0:
        return "?"
    # effective vertical resolution: the smaller dimension handles both
    # landscape (1920x1080) and portrait (1080x1920) correctly
    long_edge = min(width, height)
    for label, threshold in RES_BUCKETS:
        if long_edge >= threshold:
            return label
    return "144p"


def disk_free(path: pathlib.Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None
