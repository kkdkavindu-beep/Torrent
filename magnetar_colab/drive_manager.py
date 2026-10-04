"""Chunked copying into the mounted Google Drive (or a local staging dir).

drive.mount exposes Drive as a normal FUSE filesystem, so 'uploading' is a
file copy — we do it in chunks so progress is real, verify sizes, and skip
files that already exist with the same size (idempotent re-runs).
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil

from . import config

log = logging.getLogger(config.LOG)


class UploadError(RuntimeError):
    pass


def upload_file(src: pathlib.Path, dest_dir: pathlib.Path, *,
                progress_cb=None, cancel_check=None) -> str:
    """Copy src into dest_dir with chunked progress. Returns
    'uploaded' | 'skipped (already on Drive)'."""
    src, dest_dir = pathlib.Path(src), pathlib.Path(dest_dir)
    if not src.is_file():
        raise UploadError(f"source disappeared: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    total = src.stat().st_size
    if dest.is_file() and dest.stat().st_size == total:
        log.info("skip (already on Drive): %s", dest)
        return "skipped (already on Drive)"

    tmp = dest.with_suffix(dest.suffix + ".part")
    done = 0
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                if cancel_check is not None and cancel_check():
                    raise UploadError("cancelled")
                chunk = fin.read(config.UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                fout.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done / total if total else 1.0, done, total)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dest)  # atomic-ish rename inside the same Drive dir
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    copied = dest.stat().st_size
    if copied != total:
        dest.unlink(missing_ok=True)
        raise UploadError(f"size mismatch after copy: {copied} != {total}")
    return "uploaded"


def delete_file(path: pathlib.Path) -> None:
    """Delete a local file and clean up parent dirs left empty."""
    path = pathlib.Path(path)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete %s: %s", path, exc)
        return
    for parent in path.parents:
        if parent in (config.DOWNLOAD_DIR, config.CONVERTED_DIR, config.BASE_DIR):
            break
        try:
            parent.rmdir()  # only removes empty dirs
        except OSError:
            break


def free_space(path: pathlib.Path | None = None) -> int | None:
    return config.disk_free(path or config.BASE_DIR)


def status_summary() -> str:
    lines = []
    if config.drive_mounted():
        lines.append(f"Drive mounted: {config.DRIVE_MYDRIVE}")
        free = config.disk_free(config.DRIVE_MYDRIVE)
        if free is not None:
            lines.append(f"Drive free space: {config.human_size(free)}")
    else:
        lines.append("Drive NOT mounted - uploads will go to local staging only.")
    free_local = free_space()
    if free_local is not None:
        lines.append(f"Workspace free space: {config.human_size(free_local)}")
    return "\n".join(lines)
