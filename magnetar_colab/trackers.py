"""Tracker discovery for better download speeds.

Colab VMs are NATed (no inbound ports), so peer discovery quality decides
download speed. We merge an embedded curated list with the daily-updated
ngosang/trackerslist 'best' ranking, cached on Drive across sessions.
"""
from __future__ import annotations

import logging
import pathlib
import threading
import urllib.request

from . import config

log = logging.getLogger(config.LOG)

_lock = threading.Lock()
_trackers: list[str] = list(config.DEFAULT_TRACKERS)
_fetched_once = False


def _cache_file() -> pathlib.Path:
    return config.cache_dir() / "trackers_best.txt"


def parse_tracker_list(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "://" not in line:
            continue
        out.append(line)
    return out


def _merge(new: list[str]) -> int:
    global _trackers
    with _lock:
        existing = {t.lower() for t in _trackers}
        additions = [t for t in new if t.lower() not in existing]
        _trackers = _trackers + additions
        return len(additions)


def current() -> list[str]:
    with _lock:
        return list(_trackers)


def _load_cache() -> None:
    cache = _cache_file()
    if cache.is_file():
        try:
            added = _merge(parse_tracker_list(cache.read_text(encoding="utf-8")))
            if added:
                log.info("loaded %d cached trackers from Drive", added)
        except Exception as exc:
            log.warning("tracker cache unreadable: %s", exc)


def fetch_now(timeout: float = 10.0) -> int:
    """Fetch the live best-tracker list, merge it, cache it. Returns added count."""
    request = urllib.request.Request(config.TRACKER_LIST_URL,
                                     headers={"User-Agent": "magnetar-colab/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    trackers = parse_tracker_list(text)
    if not trackers:
        raise RuntimeError("tracker list came back empty")
    added = _merge(trackers)
    try:
        _cache_file().write_text("\n".join(trackers), encoding="utf-8")
    except Exception as exc:
        log.warning("could not cache tracker list: %s", exc)
    return added


def refresh_async(apply_cb=None) -> threading.Thread:
    """Load the Drive cache immediately, then refresh from the network in the
    background. apply_cb(trackers) is called with the merged list when done
    (used to push new trackers into torrents already in the session)."""
    global _fetched_once
    _load_cache()

    def run() -> None:
        try:
            added = fetch_now()
            log.info("tracker list refreshed from ngosang/trackerslist (+%d, %d total)",
                     added, len(current()))
            if apply_cb:
                try:
                    apply_cb(current())
                except Exception as exc:
                    log.warning("applying trackers to session failed: %s", exc)
        except Exception as exc:
            log.info("live tracker refresh unavailable (%s) - using built-in list", exc)

    if _fetched_once:
        return threading.Thread(target=run)  # already primed; caller shouldn't re-call
    _fetched_once = True
    thread = threading.Thread(target=run, daemon=True, name="tracker-refresh")
    thread.start()
    return thread
