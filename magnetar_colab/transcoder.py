"""Video conversion with a GPU-first fallback ladder for Colab T4 GPUs.

Ladder (first rung that works wins):
  1. NVDEC decode -> scale_cuda -> NVENC encode          (full GPU)
  2. CPU decode/scale -> NVENC encode                    (needs NVENC ffmpeg only)
  3. CPU decode/scale -> libx264/libx265 encode          (always works, slow)

Colab's stock ffmpeg has no NVENC, so when a GPU is present we fetch a static
BtbN build (cached on Drive across sessions). Aspect ratio is always preserved:
scale only sets the target height, width is computed automatically (-2 keeps it
even). We never upscale and never crop.
"""
from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from . import config, media_info

log = logging.getLogger(config.LOG)

_SLOW = "veryfast"


class ConversionError(RuntimeError):
    pass


class ConversionCancelled(ConversionError):
    pass


@dataclass
class GpuSpec:
    available: bool = False
    name: str = ""
    driver: str = ""
    nvenc_h264: bool = False
    nvenc_hevc: bool = False
    scale_cuda: bool = False
    ffmpeg: str = ""            # binary that should be used for conversions
    rung_label: str = "CPU"

    def describe(self) -> str:
        if not self.available:
            return "No NVIDIA GPU visible - conversions will use CPU (libx264)."
        enc = "h264" + ("/hevc" if self.nvenc_hevc else "")
        pipeline = "NVDEC+scale_cuda+NVENC (full GPU)" if self.scale_cuda else "NVENC encode (CPU decode)"
        return (f"GPU: {self.name} (driver {self.driver})\n"
                f"NVENC encoders: {enc} | pipeline: {pipeline}\n"
                f"ffmpeg: {self.ffmpeg}")


_GPU_CACHE: dict | None = None
_GPU_LOCK = threading.Lock()


# ------------------------------------------------------------------ detection
def _nvidia_smi() -> tuple[str, str] | None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if not out:
            return None
        name, _, driver = out.partition(",")
        return name.strip(), driver.strip()
    except Exception:
        return None


def _test_encoder(ffmpeg: str, encoder: str) -> bool:
    """Tiny synthetic encode to prove the encoder actually works on this GPU/driver."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=duration=0.1:size=256x256", "-frames:v", "5",
             "-c:v", encoder, "-f", "null", "-"],
            capture_output=True, timeout=60,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _has_filter(ffmpeg: str, name: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30).stdout
        return name in out
    except Exception:
        return False


def setup_gpu(force_recheck: bool = False) -> GpuSpec:
    """Detect GPU + pick the best ffmpeg binary; result is cached per session."""
    global _GPU_CACHE
    with _GPU_LOCK:
        if _GPU_CACHE is not None and not force_recheck:
            return _GPU_CACHE["spec"]

        gpu = GpuSpec()
        detected = _nvidia_smi()
        if detected:
            gpu.available, gpu.name, gpu.driver = True, detected[0], detected[1]

        candidates: list[str] = []
        which = shutil.which("ffmpeg")
        if which:
            candidates.append(which)
        try:
            _, ffprobe = media_info.ensure_ffmpeg()
            cached_ffmpeg = str(pathlib.Path(ffprobe).with_name(
                "ffmpeg.exe" if pathlib.Path(ffprobe).suffix == ".exe" else "ffmpeg"))
            if cached_ffmpeg not in candidates and pathlib.Path(cached_ffmpeg).is_file():
                candidates.append(cached_ffmpeg)
        except Exception:
            pass

        for binary in candidates:
            if _test_encoder(binary, "h264_nvenc"):
                gpu.ffmpeg = binary
                gpu.nvenc_h264 = True
                gpu.nvenc_hevc = _test_encoder(binary, "hevc_nvenc")
                gpu.scale_cuda = _has_filter(binary, "scale_cuda")
                gpu.rung_label = "NVENC"
                break

        if gpu.available and not gpu.nvenc_h264:
            # GPU present but no working NVENC binary -> fetch the BtbN build
            try:
                ffmpeg_path, _ = media_info.ensure_ffmpeg(
                    cache_dir=config.ffmpeg_cache_dir(), force_download=True)
                if _test_encoder(ffmpeg_path, "h264_nvenc"):
                    gpu.ffmpeg = ffmpeg_path
                    gpu.nvenc_h264 = True
                    gpu.nvenc_hevc = _test_encoder(ffmpeg_path, "hevc_nvenc")
                    gpu.scale_cuda = _has_filter(ffmpeg_path, "scale_cuda")
                    gpu.rung_label = "NVENC"
                    log.info("NVENC enabled via downloaded static ffmpeg: %s", ffmpeg_path)
                else:
                    log.warning("NVENC test failed even with static ffmpeg - using CPU.")
            except Exception as exc:
                log.warning("static ffmpeg fetch failed (%s) - using CPU.", exc)

        if not gpu.nvenc_h264:
            # CPU conversions still need a binary: prefer system ffmpeg, else the
            # cached/downloaded static build (ensure_ffmpeg downloads only if
            # neither exists).
            if not gpu.ffmpeg:
                try:
                    ffmpeg_path, _ = media_info.ensure_ffmpeg()
                    gpu.ffmpeg = ffmpeg_path
                except Exception as exc:
                    log.warning("no ffmpeg available (%s) - conversions will fail", exc)
            gpu.rung_label = "CPU"

        _GPU_CACHE = {"spec": gpu}
        log.info("gpu detection: %s", gpu.describe().replace("\n", " | "))
        return gpu


# ------------------------------------------------------------------ conversion
def choose_sub_policy(sub_codecs: list[str], container: str) -> str:
    """'copy' when the target container can hold every subtitle stream, else 'none'."""
    if not sub_codecs:
        return "none"
    if container == "mkv":
        return "copy"
    if container in ("mp4", "mov", "m4v"):
        text_subs = {"srt", "subrip", "ass", "ssa", "mov_text", "text"}
        return "copy" if all(c in text_subs for c in sub_codecs) else "none"
    return "none"


def _target_container(source_ext: str, container_mode: str) -> str:
    if container_mode != "keep":
        return container_mode
    # containers that are a poor home for re-encoded modern streams
    legacy = {"avi", "wmv", "flv", "mpg", "mpeg", "vob", "asf", "3gp", "rm",
              "rmvb", "divx", "ts", "m2ts", "mts", "ogv", "f4v"}
    return "mp4" if source_ext.lstrip(".") in legacy else source_ext.lstrip(".") or "mkv"


def _needs_aac(audio_codecs: list[str], audio_mode: str, container: str) -> tuple[bool, str]:
    if audio_mode == "aac":
        return True, "AAC requested"
    if container in ("mp4", "mov", "m4v"):
        for codec in audio_codecs:
            if any(codec.startswith(prefix) or prefix in codec for prefix in config.MP4_INCOMPATIBLE_AUDIO):
                return True, f"{codec.upper()} is not MP4-safe -> AAC"
    return False, ""


def _build_args(rung: str, src: pathlib.Path, dst: pathlib.Path, target_h: int,
                *, vcodec_target: str, container: str, sub_policy: str,
                force_aac: bool, ffmpeg: str) -> list[str]:
    ext = dst.suffix.lstrip(".")
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]

    if rung == "full-gpu":
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(src)]
        scale = f"scale_cuda=w=-2:h={target_h}:format=nv12"
    elif rung == "nvenc":
        args += ["-i", str(src)]
        scale = f"scale=w=-2:h={target_h}:format=nv12"
    else:  # cpu
        args += ["-i", str(src)]
        scale = f"scale=w=-2:h={target_h}"

    args += ["-map", "0:v:0", "-map", "0:a?"]

    if rung in ("full-gpu", "nvenc"):
        if vcodec_target == "hevc":
            args += ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "24", "-b:v", "0"]
        else:
            args += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    else:
        if vcodec_target == "hevc":
            args += ["-c:v", "libx265", "-preset", _SLOW, "-crf", "24"]
        else:
            args += ["-c:v", "libx264", "-preset", _SLOW, "-crf", "22"]

    args += ["-vf", scale]

    if force_aac:
        args += ["-c:a", "aac", "-b:a", config.AUDIO_BITRATE]
    else:
        args += ["-c:a", "copy"]

    if sub_policy == "copy":
        if container == "mkv":
            args += ["-map", "0:s?", "-c:s", "copy"]
        else:
            args += ["-map", "0:s?", "-c:s", "mov_text"]
    else:
        args += ["-sn"]

    args += ["-map_metadata", "0"]
    if ext in ("mp4", "mov", "m4v"):
        args += ["-movflags", "+faststart"]
    args.append(str(dst))
    return args


def _run_with_progress(cmd: list[str], duration_sec: float | None, progress_cb,
                       cancel_check=None) -> None:
    stderr_file = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ConversionError(f"could not launch ffmpeg: {exc}") from exc

    out_time_us = None
    speed = None
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if cancel_check is not None and cancel_check():
                proc.terminate()
                raise ConversionCancelled("cancelled by user")
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_us" or key == "out_time_ms":
                try:
                    out_time_us = int(value)
                except ValueError:
                    pass
            elif key == "speed":
                speed = value
                if progress_cb and duration_sec and out_time_us:
                    progress_cb(min(0.99, out_time_us / 1e6 / duration_sec), speed)
            elif key == "progress" and value == "end" and progress_cb:
                progress_cb(1.0, speed)
        proc.wait(timeout=300)
    finally:
        if proc.poll() is None:  # safety net for exceptions above
            proc.kill()
            proc.wait(timeout=30)

    if proc.returncode != 0:
        stderr_file.seek(0)
        tail = stderr_file.read().decode("utf-8", "replace").strip().splitlines()[-3:]
        raise ConversionError(" | ".join(tail) or f"ffmpeg exited with {proc.returncode}")


def verify_output(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Re-probe the converted file; raise if it looks broken. Never trust ffmpeg's exit code alone."""
    if not dst.is_file() or dst.stat().st_size == 0:
        raise ConversionError("output file missing or empty")
    src_info = media_info.probe(src)
    dst_info = media_info.probe(dst)
    if dst_info.error:
        raise ConversionError(f"output probe failed: {dst_info.error}")
    if not dst_info.vcodec:
        raise ConversionError("output has no video stream")
    if src_info.duration_sec and dst_info.duration_sec:
        tolerance = max(2.0, 0.05 * src_info.duration_sec)
        if abs(dst_info.duration_sec - src_info.duration_sec) > tolerance:
            raise ConversionError(
                f"duration mismatch: src {src_info.duration_sec:.1f}s vs out {dst_info.duration_sec:.1f}s")


def convert(src: pathlib.Path, dst: pathlib.Path, target_h: int, *,
            info=None, vcodec_target: str = "h264", audio_mode: str = "copy",
            container_mode: str = "keep", gpu: GpuSpec | None = None,
            progress_cb=None, cancel_check=None) -> pathlib.Path:
    """Convert one video, walking the GPU->CPU ladder. Returns dst on success."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if info is None:
        info = media_info.probe(src)
    if info.height:
        target_h = min(target_h, info.height)  # never upscale

    container = _target_container(src.suffix, container_mode)
    dst = dst.with_suffix(f".{container}")
    sub_policy = choose_sub_policy(getattr(info, "sub_codecs", []) or [], container)
    force_aac, aac_note = _needs_aac(info.audio_codecs, audio_mode, container)
    if aac_note:
        log.info("%s: %s", src.name, aac_note)

    gpu = gpu or setup_gpu()
    rungs: list[tuple[str, str]] = []
    if gpu.nvenc_h264 and gpu.scale_cuda:
        rungs.append(("full-gpu", "NVENC full-GPU (NVDEC+scale_cuda)"))
    if gpu.nvenc_h264:
        rungs.append(("nvenc", "NVENC encode (CPU decode)"))
    rungs.append(("cpu", "CPU libx264/x265"))

    errors = []
    for rung, label in rungs:
        if rung != "cpu" and vcodec_target == "hevc" and not gpu.nvenc_hevc:
            continue  # HEVC target without hevc_nvenc falls through to CPU
        cmd = _build_args(rung, src, dst, target_h, vcodec_target=vcodec_target,
                          container=container, sub_policy=sub_policy,
                          force_aac=force_aac, ffmpeg=gpu.ffmpeg or "ffmpeg")
        log.info("[%s] %s -> %s", label, src.name, dst.name)
        started = time.time()
        try:
            _run_with_progress(cmd, info.duration_sec, progress_cb, cancel_check)
            verify_output(src, dst)
            log.info("[%s] done in %.1fs: %s (%s)", label, time.time() - started,
                     dst.name, config.human_size(dst.stat().st_size))
            return dst
        except ConversionCancelled:
            dst.unlink(missing_ok=True)
            raise
        except ConversionError as exc:
            errors.append(f"{label}: {exc}")
            log.warning("[%s] failed: %s - trying next rung", label, exc)
            dst.unlink(missing_ok=True)

    raise ConversionError("all rungs failed: " + " ;; ".join(errors))
