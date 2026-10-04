"""Orchestrates: scan downloaded files -> plan (copy vs convert) -> execute.

Execution runs two workers at a time so an NVENC encode of one file overlaps
with the Drive upload of the previous one (T4 has one encoder chip; Drive I/O
is the real bottleneck).
"""
from __future__ import annotations

import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, drive_manager, media_info, transcoder

log = logging.getLogger(config.LOG)

NO_CONVERSION = "No conversion (copy all)"


@dataclass
class PlanRow:
    key: str
    path: pathlib.Path
    name: str
    media_type: str          # video | audio | subtitle | other
    container: str
    vcodec_label: str
    resolution: str
    audio_summary: str
    size: int
    action: str              # copy | convert | skip
    target_h: int            # 0 when copying
    action_label: str        # display text
    note: str = ""


def _torrent_subdir(path: pathlib.Path) -> str:
    try:
        parts = path.resolve().relative_to(config.DOWNLOAD_DIR.resolve()).parts
    except ValueError:
        return "root"
    return parts[0] if len(parts) > 1 else "root"


def _build_row(info: media_info.MediaInfo, target_h: int | None) -> PlanRow:
    key = str(info.path)
    base = PlanRow(
        key=key, path=info.path, name=info.name, media_type=info.media_type,
        container=info.container, vcodec_label=info.vcodec_label or "-",
        resolution=info.resolution if info.media_type == "video" else "-",
        audio_summary=info.audio_summary, size=info.size,
        action="copy", target_h=0, action_label="", note="",
    )
    if info.media_type == "video" and info.error:
        base.action = "skip"
        base.action_label = "SKIP (unreadable)"
        base.note = info.error
        return base
    if info.media_type == "video":
        if info.error or info.height == 0:
            base.action = "skip"
            base.action_label = "SKIP (unreadable)"
            base.note = info.error or "no video stream"
            return base
        if target_h and info.height > target_h:
            base.action = "convert"
            base.target_h = target_h
            src_label = config.resolution_label(info.width, info.height)
            base.action_label = f"CONVERT {src_label} > {config.resolution_label(info.width, target_h)}"
        else:
            base.action = "copy"
            base.action_label = "COPY"
        return base
    if info.media_type == "audio":
        base.action_label = "COPY (audio)"
        return base
    if info.media_type == "subtitle":
        base.action_label = "COPY (subtitle)"
        return base
    base.action_label = "COPY (other)"
    return base


class Pipeline:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self.running = False
        self._cancel = threading.Event()
        self.plan: list[PlanRow] = []
        self.last_scan_summary = ""

    # ------------------------------------------------------------------ scan
    def scan(self, target_label: str | None) -> tuple[list[PlanRow], list[str], list[str]]:
        """Probe everything in the download dir. Returns (rows, default_selected_keys,
        selectable_keys). target_label None/NO_CONVERSION -> everything copies."""
        target_h = None if target_label in (None, NO_CONVERSION) else config.TARGET_HEIGHTS[target_label]
        found = media_info.scan_workspace()
        rows: list[PlanRow] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            video_rows = list(pool.map(lambda v: _build_row(v, target_h), found["videos"]))
            audio_rows = list(pool.map(lambda a: _build_row(a, None), found["audio"]))
        rows.extend(video_rows)
        rows.extend(audio_rows)
        for sub in found["subtitles"]:
            rows.append(_build_row(media_info.probe(sub), None))

        with self._lock:
            self.plan = rows
        selected = [r.key for r in rows if r.media_type in ("video", "audio")]
        convertible = sum(1 for r in rows if r.action == "convert")
        self.last_scan_summary = (
            f"{len(found['videos'])} video(s), {len(found['audio'])} audio, "
            f"{len(found['subtitles'])} subtitle(s), {len(found['other'])} other file(s); "
            f"{convertible} would be converted."
        )
        log.info("scan: %s", self.last_scan_summary)
        return rows, selected, [r.key for r in rows]

    # --------------------------------------------------------------- execute
    def execute(self, selected_keys: list[str], *, drive_folder: str,
                delete_after: bool, vcodec_target: str, audio_mode: str,
                container_mode: str) -> str:
        if self.running:
            return "A job is already running - wait for it to finish."
        rows = [r for r in self.plan if r.key in set(selected_keys) and r.action != "skip"]
        if not rows:
            return "Nothing selected (or scan produced no files). Run a scan first."
        if not config.drive_mounted():
            return "Google Drive is not mounted. Re-run the notebook's first cell."

        self._cancel.clear()
        self.running = True
        with self._lock:
            self._jobs = {r.key: {
                "name": r.name, "stage": "Queued", "pct": None, "speed": "",
                "size": r.size, "message": r.action_label,
            } for r in rows}
        gpu = transcoder.setup_gpu()
        threading.Thread(target=self._worker, args=(rows, drive_folder, delete_after,
                                                    vcodec_target, audio_mode, container_mode, gpu),
                         daemon=True, name="pipeline").start()
        return (f"Started {len(rows)} job(s) using {gpu.rung_label}. "
                f"Destination: Drive/{drive_folder}/")

    def cancel(self) -> str:
        if self.running:
            self._cancel.set()
            return "Cancelling after the current chunk..."
        return "Nothing running."

    def _set_job(self, key: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job:
                job.update(fields)

    def _worker(self, rows: list[PlanRow], drive_folder: str, delete_after: bool,
                vcodec_target: str, audio_mode: str, container_mode: str,
                gpu: transcoder.GpuSpec) -> None:
        started = time.time()
        try:
            with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_CONVERTS) as pool:
                list(pool.map(lambda row: self._process_row(
                    row, drive_folder, delete_after, vcodec_target, audio_mode,
                    container_mode, gpu), rows))
        finally:
            self.running = False
            log.info("pipeline finished in %.1fs", time.time() - started)

    def _process_row(self, row: PlanRow, drive_folder: str, delete_after: bool,
                     vcodec_target: str, audio_mode: str, container_mode: str,
                     gpu: transcoder.GpuSpec) -> None:
        key = row.key
        try:
            upload_src = row.path
            converted: pathlib.Path | None = None

            if row.action == "convert":
                self._set_job(key, stage="Converting", pct=0.0)
                stem = row.path.stem
                container = transcoder._target_container(row.path.suffix, container_mode)
                dst = config.CONVERTED_DIR / f"{stem}.{container}"
                converted = transcoder.convert(
                    row.path, dst, row.target_h, vcodec_target=vcodec_target,
                    audio_mode=audio_mode, container_mode=container_mode, gpu=gpu,
                    progress_cb=lambda pct, speed: self._set_job(
                        key, pct=pct, speed=f"{speed}x" if speed else ""),
                    cancel_check=self._cancel.is_set,
                )
                upload_src = converted
                self._set_job(key, stage="Uploading", pct=0.0, speed="")

            dest_dir = config.drive_base(drive_folder) / _torrent_subdir(row.path)
            self._set_job(key, stage="Uploading", pct=0.0)

            def up_cb(pct: float, done: int, total: int) -> None:
                self._set_job(key, pct=pct,
                              speed=f"{config.human_size(done)} / {config.human_size(total)}")

            result = drive_manager.upload_file(upload_src, dest_dir,
                                               progress_cb=up_cb,
                                               cancel_check=self._cancel.is_set)
            if result.startswith("skipped"):
                self._set_job(key, stage="Skipped", pct=1.0, message=result)
            else:
                self._set_job(key, stage="Done", pct=1.0, speed="", message="On Drive")

            if delete_after:
                drive_manager.delete_file(row.path)
                if converted is not None:
                    drive_manager.delete_file(converted)
                log.info("deleted local copies of %s", row.name)
        except Exception as exc:
            log.exception("job failed: %s", row.name)
            self._set_job(key, stage="Failed", message=str(exc)[:200])

    # ------------------------------------------------------------------- ui
    def job_rows(self) -> list[list]:
        with self._lock:
            jobs = list(self._jobs.values())
        rows = []
        for job in jobs:
            pct = job["pct"]
            pct_text = f"{pct * 100:.0f}%" if pct is not None else "-"
            rows.append([job["name"], job["stage"], pct_text, job["speed"],
                         config.human_size(job["size"]), job["message"]])
        return rows
