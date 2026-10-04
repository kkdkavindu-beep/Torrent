"""Media inspection via ffprobe + automatic static ffmpeg acquisition.

Colab ships a stock ffmpeg (good enough for probing, no NVENC), so downloads
only happen when a GPU is present (see transcoder.setup_gpu) or when running
somewhere with no ffmpeg at all. Builds come from BtbN/FFmpeg-Builds, which
include ffmpeg + ffprobe with NVENC enabled.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config

log = logging.getLogger(config.LOG)

BTBN_URLS = {
    ("Linux", "x86_64"): "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-linux64-gpl.tar.xz",
    ("Windows", "x86_64"): "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
}


def _platform_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86-64", "x64"):
        machine = "x86_64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64"
    return platform.system(), machine

VCODEC_LABELS = {
    "h264": "H.264/AVC", "hevc": "H.265/HEVC", "h265": "H.265/HEVC", "av1": "AV1",
    "vp9": "VP9", "vp8": "VP8", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1", "msmpeg4v3": "WMV", "flv1": "FLV", "mjpeg": "MJPEG",
    "dvvideo": "DV", "theora": "Theora", "wmv3": "WMV", "wmv2": "WMV",
}


@dataclass
class MediaInfo:
    path: pathlib.Path
    media_type: str = "other"        # video | audio | subtitle | other
    container: str = ""              # mp4 / mkv / avi ...
    duration_sec: float | None = None
    vcodec: str | None = None        # ffprobe codec name, e.g. h264 / hevc
    vcodec_label: str | None = None  # H.264/AVC ...
    width: int = 0
    height: int = 0
    fps: float | None = None
    bitrate_bps: int | None = None
    pix_fmt: str | None = None
    audio_summary: str = "-"         # "AAC 5.1 (eng) + AC3 2.0"
    audio_codecs: list[str] = field(default_factory=list)
    sub_codecs: list[str] = field(default_factory=list)
    n_subtitle: int = 0
    size: int = 0
    resolution: str = "?"            # 1080p / 720p ...
    error: str | None = None

    @property
    def name(self) -> str:
        return self.path.name


# ------------------------------------------------------------------ ffmpeg bin
def _marker_path(cache: pathlib.Path) -> pathlib.Path:
    return cache / "ffmpeg_build.json"


def _load_cached_build(cache: pathlib.Path):
    marker = _marker_path(cache)
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text())
        ffmpeg, ffprobe = pathlib.Path(data["ffmpeg"]), pathlib.Path(data["ffprobe"])
        if ffmpeg.is_file() and ffprobe.is_file():
            return str(ffmpeg), str(ffprobe)
    except Exception:
        pass
    return None


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    log.info("downloading %s (%.0f MB)", url.rsplit("/", 1)[-1], 0)
    request = urllib.request.Request(url, headers={"User-Agent": "magnetar-colab/1.0"})

    def hook(blocks: int, block_size: int, total: int) -> None:
        state["done"] = blocks * block_size / 1e6
        if total > 0 and state["done"] - state["last"] >= 25.0:
            state["last"] = state["done"]
            log.info("  ffmpeg download: %.0f / %.0f MB", state["done"], total / 1e6)

    state = {"done": 0.0, "last": -25.0}
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.replace(dest)
    return dest


def _extract(archive: pathlib.Path, cache: pathlib.Path) -> tuple[str, str]:
    extract_dir = cache / "build"
    extract_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
    else:  # .tar.xz
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(extract_dir)

    exe = ".exe" if platform.system() == "Windows" else ""
    ffmpeg = ffprobe = None
    for candidate in extract_dir.rglob(f"ffmpeg{exe}"):
        ffmpeg = candidate
        break
    for candidate in extract_dir.rglob(f"ffprobe{exe}"):
        ffprobe = candidate
        break
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg binaries not found inside downloaded build")
    for binary in (ffmpeg, ffprobe):
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    marker = {"ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe)}
    _marker_path(cache).write_text(json.dumps(marker))
    return str(ffmpeg), str(ffprobe)


def ensure_ffmpeg(cache_dir: pathlib.Path | None = None, force_download: bool = False) -> tuple[str, str]:
    """Return (ffmpeg, ffprobe) paths. Prefers system installs; otherwise
    downloads a static build into cache_dir."""
    cache = cache_dir or config.ffmpeg_cache_dir()
    if not force_download:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if ffmpeg and ffprobe:
            return ffmpeg, ffprobe
    cached = _load_cached_build(cache)
    if cached:
        return cached

    key = _platform_key()
    url = BTBN_URLS.get(key)
    if url is None:
        raise RuntimeError(f"No prebuilt ffmpeg available for {key}; install ffmpeg+ffprobe manually.")
    archive_name = url.rsplit("/", 1)[-1]
    archive = cache / archive_name
    if not archive.is_file():
        _download(url, archive)
    return _extract(archive, cache)


# ---------------------------------------------------------------------- probe
def _parse_fraction(text: str | None) -> float | None:
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else None
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def _channels_label(channels: int | None) -> str:
    return {1: "1.0", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"}.get(channels or 0, f"{channels or '?'}ch")


def probe(path: pathlib.Path | str, ffprobe_path: str | None = None) -> MediaInfo:
    path = pathlib.Path(path)
    ext = path.suffix.lower()
    info = MediaInfo(path=path, size=path.stat().st_size if path.exists() else 0,
                     container=ext.lstrip("."))
    if ext in config.VIDEO_EXTS:
        info.media_type = "video"
    elif ext in config.AUDIO_EXTS:
        info.media_type = "audio"
    elif ext in config.SUBTITLE_EXTS:
        info.media_type = "subtitle"
        return info
    else:
        info.media_type = "other"
        return info

    ffprobe_path = ffprobe_path or shutil.which("ffprobe")
    if not ffprobe_path:
        try:
            _, ffprobe_path = ensure_ffmpeg()
        except Exception as exc:
            info.error = f"ffprobe unavailable: {exc}"
            return info

    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        info.error = f"ffprobe failed: {exc}"
        return info
    if proc.returncode != 0:
        info.error = (proc.stderr or "unknown ffprobe error").strip().splitlines()[-1][:200]
        return info

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        info.error = "ffprobe returned unparseable output"
        return info

    fmt = data.get("format", {})
    try:
        info.duration_sec = float(fmt.get("duration")) if fmt.get("duration") else None
    except (TypeError, ValueError):
        pass
    try:
        info.bitrate_bps = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        pass

    audio_parts, video_stream, cover_stream = [], None, None
    for stream in data.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype == "video":
            # cover-art / thumbnail streams never win over real video
            if stream.get("disposition", {}).get("attached_pic") == 1 or \
                    stream.get("codec_name") in ("mjpeg", "png", "bmp", "gif"):
                cover_stream = cover_stream or stream
                continue
            video_stream = video_stream or stream
        elif ctype == "audio":
            codec = stream.get("codec_name", "?")
            info.audio_codecs.append(codec)
            lang = stream.get("tags", {}).get("language", "")
            channels = stream.get("channels")
            audio_parts.append(
                f"{codec.upper()} {_channels_label(channels)}" + (f" ({lang})" if lang else "")
            )
        elif ctype == "subtitle":
            info.n_subtitle += 1
            info.sub_codecs.append(stream.get("codec_name", "?"))
    video_stream = video_stream or cover_stream

    if video_stream is not None and info.media_type == "video":
        info.vcodec = video_stream.get("codec_name")
        info.vcodec_label = VCODEC_LABELS.get(info.vcodec or "", (info.vcodec or "?").upper())
        info.width = int(video_stream.get("width") or 0)
        info.height = int(video_stream.get("height") or 0)
        info.fps = _parse_fraction(video_stream.get("avg_frame_rate")) or _parse_fraction(video_stream.get("r_frame_rate"))
        info.pix_fmt = video_stream.get("pix_fmt")
        info.resolution = config.resolution_label(info.width, info.height)
    elif info.media_type == "video" and video_stream is None:
        info.error = "no video stream found"

    info.audio_summary = " + ".join(audio_parts) if audio_parts else "-"
    return info


def probe_many(paths: list[pathlib.Path], ffprobe_path: str | None = None,
               workers: int = 4) -> list[MediaInfo]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda p: probe(p, ffprobe_path), paths))


def scan_workspace(download_dir: pathlib.Path | None = None) -> dict:
    """Scan the download directory and return {'videos': [...], 'audio': [...],
    'subtitles': [...], 'other': [...]} as MediaInfo / Path objects."""
    root = pathlib.Path(download_dir or config.DOWNLOAD_DIR)
    result = {"videos": [], "audio": [], "subtitles": [], "other": []}
    if not root.is_dir():
        return result

    media_paths: list[pathlib.Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.name.endswith(".part") or path.suffix == ".torrent":
            continue
        ext = path.suffix.lower()
        if ext in config.VIDEO_EXTS or ext in config.AUDIO_EXTS:
            media_paths.append(path)
        elif ext in config.SUBTITLE_EXTS:
            result["subtitles"].append(path)
        else:
            result["other"].append(path)

    if media_paths:
        _, ffprobe_path = ensure_ffmpeg()
        infos = probe_many(media_paths, ffprobe_path)
        for info in infos:
            if info.media_type == "video":
                result["videos"].append(info)
            elif info.media_type == "audio":
                result["audio"].append(info)
    return result
