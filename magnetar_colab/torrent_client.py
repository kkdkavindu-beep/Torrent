"""libtorrent session wrapper.

Ported patterns from Magnetar (Android):
- probe-then-pause metadata flow (add magnet -> poll for metadata -> pause so
  no payload is fetched until the user picks files -> adopt or discard)
- paused torrents must clear AUTO_MANAGED, otherwise libtorrent's queue
  manager resumes them behind our back
- progress = continuous polling (1 s), discrete events (metadata/error) via
  the alert loop
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

from . import config, magnets

log = logging.getLogger(config.LOG)

try:
    import libtorrent as lt
    HAVE_LIBTORRENT = True
except Exception:  # pragma: no cover - surfaces nicely in the UI
    lt = None
    HAVE_LIBTORRENT = False

_ALERT_CATEGORY = None
if HAVE_LIBTORRENT:
    _ALERT_CATEGORY = getattr(lt.alert, "category_t", None) or getattr(lt, "alert_category", None)


def _alert_mask() -> int:
    if not HAVE_LIBTORRENT:
        return 0
    category = getattr(lt, "alert_category", None)  # libtorrent 2.1+
    if category is not None:
        try:
            return int(category.status) | int(category.error)
        except Exception:
            pass
    category = getattr(lt.alert, "category_t", None)  # libtorrent 2.0
    if category is not None:
        try:
            return int(category.status_notification) | int(category.error_notification)
        except Exception:
            pass
    return 0


def _make_session(settings: dict):
    """Create a session across binding generations: 2.1+ takes a plain dict,
    2.0 needs a settings_pack with enum keys."""
    if hasattr(lt, "session_params"):
        try:
            return lt.session(dict(settings))
        except Exception:
            params = lt.session_params()
            merged = dict(lt.default_settings())
            merged.update(settings)
            params.settings = merged
            return lt.session(params)
    sp = lt.settings_pack()
    for key, value in settings.items():
        enum = getattr(lt.settings_pack, key, None)
        if enum is None:
            continue
        if isinstance(value, bool):
            sp.set_bool(enum, value)
        elif isinstance(value, int):
            sp.set_int(enum, value)
        else:
            sp.set_str(enum, value)
    return lt.session(sp)


# torrent_status enum members differ between binding generations
_STATE_LABELS = {}
if HAVE_LIBTORRENT:
    for _name, _label in [
        ("checking_files", "CHECKING"),
        ("downloading_metadata", "FETCHING METADATA"),
        ("downloading", "DOWNLOADING"),
        ("finished", "FINISHED"),
        ("seeding", "SEEDING"),
        ("allocating_files", "ALLOCATING"),
        ("checking_resume_data", "CHECKING"),
    ]:
        _member = getattr(lt.torrent_status, _name, None)
        if _member is not None:
            _STATE_LABELS[_member] = _label


def _sha1_hex(ih) -> str:
    """Best-effort info-hash -> 40-char hex string across libtorrent versions."""
    for attr in ("to_bytes", "string"):
        fn = getattr(ih, attr, None)
        if fn is None:
            continue
        try:
            data = bytes(fn())
            if len(data) == 20:
                return data.hex()
        except Exception:
            continue
    text = str(ih)
    if len(text) == 40:
        return text.lower()
    raise RuntimeError(f"cannot stringify info hash: {text!r}")


@dataclass
class TorrentFileEntry:
    index: int
    path: str
    size: int
    priority: int = 4
    progress: float = 0.0

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class ProbeResult:
    info_hash: str
    name: str
    files: list[TorrentFileEntry]
    total_size: int
    peers: int


@dataclass
class TorrentSnapshot:
    info_hash: str
    name: str
    state: str
    progress: float
    total_wanted: int
    total_wanted_done: int
    download_rate: int
    num_seeds: int
    num_peers: int
    eta_sec: float | None
    files: list[TorrentFileEntry] = field(default_factory=list)


def _state_label(status) -> str:
    if not HAVE_LIBTORRENT:
        return "?"
    errc = getattr(status, "errc", None)
    if errc is not None:
        try:
            if errc.value() != 0:
                return "ERROR: " + errc.message()
        except Exception:
            pass
    try:
        if status.flags & lt.torrent_flags.paused:
            return "PAUSED"
    except Exception:
        pass
    return _STATE_LABELS.get(status.state, str(status.state))


class TorrentClient:
    """One libtorrent session per Colab VM."""

    def __init__(self) -> None:
        self._session = None
        self._handles: dict[str, object] = {}  # info_hash -> torrent_handle
        self._names: dict[str, str] = {}
        self._lock = threading.RLock()
        self._alert_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not HAVE_LIBTORRENT:
            raise RuntimeError(
                "libtorrent is not installed. Re-run the notebook setup cell "
                "(pip install libtorrent)."
            )
        self._ensure_session()

    def _ensure_session(self):
        with self._lock:
            if self._session is not None:
                return self._session
            port = random.randint(40000, 60000)  # Magnetar's default port range
            settings = {
                "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": False,   # Colab is NATed; pointless
                "enable_natpmp": False,
                "enable_outgoing_utp": True,
                "alert_mask": _alert_mask(),
                "user_agent": "MagnetarColab/1.0",
            }
            self._session = _make_session(settings)
            self._stop.clear()
            self._alert_thread = threading.Thread(target=self._alert_loop, daemon=True, name="lt-alerts")
            self._alert_thread.start()
            log.info("libtorrent session started (listen port %s)", port)
            return self._session

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            if self._session is not None:
                self._session.pause()
                self._session = None
            self._handles.clear()

    def _alert_loop(self) -> None:
        while not self._stop.is_set():
            session = self._session
            if session is None:
                return
            try:
                for alert in session.pop_alerts():
                    msg = getattr(alert, "message", lambda: "")()
                    if not msg:
                        continue
                    name = type(alert).__name__
                    if "error" in name.lower():
                        log.warning("libtorrent: %s: %s", name, msg)
                    elif name in ("metadata_received_alert", "torrent_finished_alert"):
                        log.info("libtorrent: %s", msg)
            except Exception as exc:  # never let the alert loop die
                log.warning("alert loop error: %s", exc)
            time.sleep(0.5)

    # -------------------------------------------------------------- add/probe
    def _add_magnet(self, magnet_uri: str):
        session = self._ensure_session()
        atp = lt.parse_magnet_uri(magnet_uri)
        atp.save_path = str(config.DOWNLOAD_DIR)
        atp.flags |= lt.torrent_flags.auto_managed
        # no payload until the user explicitly picks files (metadata still flows)
        atp.flags |= getattr(lt.torrent_flags, "default_dont_download", 0)
        try:
            return session.add_torrent(atp)
        except RuntimeError:
            # duplicate add -> reuse the existing handle
            ih_hex = magnets.info_hash(magnet_uri)
            if ih_hex and ih_hex in self._handles:
                return self._handles[ih_hex]
            raise

    def probe_metadata(self, magnet_uri: str, progress_cb=None) -> ProbeResult:
        """Fetch metadata for a magnet and pause the torrent before any payload
        downloads. Mirrors Magnetar's DownloadRepository.probeMetadata."""
        if not magnets.is_magnet(magnet_uri or ""):
            raise ValueError("That doesn't look like a magnet link (expected magnet:?xt=...).")
        ih = magnets.info_hash(magnet_uri)
        if ih is None:
            raise ValueError("Magnet link has no valid btih info hash.")
        with self._lock:
            handle = self._handles.get(ih)
            if handle is None:
                handle = self._add_magnet(magnets.augment(magnet_uri))
                self._handles[ih] = handle

        started = time.time()
        deadline = started + config.PROBE_TIMEOUT_SEC
        while time.time() < deadline:
            status = handle.status()
            if handle.torrent_file() is not None:
                break
            if progress_cb:
                elapsed = time.time() - started
                progress_cb(min(0.95, elapsed / config.PROBE_TIMEOUT_SEC), status.num_peers)
            time.sleep(config.PROBE_POLL_SEC)
        else:
            peers = handle.status().num_peers
            self._remove(ih, delete_files=False)
            raise TimeoutError(
                f"No metadata after {int(config.PROBE_TIMEOUT_SEC)}s "
                f"({peers} peers). The torrent may be dead - try again or use another source."
            )

        ti = handle.torrent_file()
        name = ti.name()
        fs = ti.files()
        files = [
            TorrentFileEntry(index=i, path=fs.file_path(i), size=fs.file_size(i))
            for i in range(fs.num_files())
        ]
        total = sum(f.size for f in files)

        # pause so nothing downloads while the user selects files (Magnetar rule:
        # clear AUTO_MANAGED whenever we pause explicitly)
        handle.unset_flags(lt.torrent_flags.auto_managed)
        handle.pause()
        self._names[ih] = name
        peers = handle.status().num_peers
        if progress_cb:
            progress_cb(1.0, peers)
        return ProbeResult(info_hash=ih, name=name, files=files, total_size=total, peers=peers)

    def files(self, info_hash: str) -> list[TorrentFileEntry]:
        handle = self._handles.get(info_hash)
        if handle is None:
            return []
        ti = handle.torrent_file()
        if ti is None:
            return []
        fs = ti.files()
        try:
            priorities = [int(p) for p in handle.file_priorities()]
        except Exception:
            priorities = [4] * fs.num_files()
        try:
            done = handle.file_progress()
        except Exception:
            done = [0] * fs.num_files()
        out = []
        for i in range(fs.num_files()):
            size = fs.file_size(i)
            out.append(TorrentFileEntry(
                index=i, path=fs.file_path(i), size=size,
                priority=priorities[i] if i < len(priorities) else 4,
                progress=(done[i] / size) if i < len(done) and size else 0.0,
            ))
        return out

    # -------------------------------------------------------------- selection
    def start_download(self, info_hash: str, selected_indices: set[int]) -> str:
        handle = self._handles.get(info_hash)
        if handle is None:
            raise RuntimeError("Torrent is no longer in the session - re-fetch the file list.")
        ti = handle.torrent_file()
        if ti is None:
            raise RuntimeError("Metadata not ready yet - wait a moment and retry.")
        fs = ti.files()
        wanted = 0
        for i in range(fs.num_files()):
            priority = 4 if i in selected_indices else 0
            handle.file_priority(i, priority)
            if priority:
                wanted += fs.file_size(i)
        if not wanted:
            raise ValueError("No files selected.")
        handle.set_flags(lt.torrent_flags.auto_managed)
        handle.resume()
        log.info("download started for %s (%d files, %s selected)",
                 self._names.get(info_hash, info_hash), len(selected_indices),
                 config.human_size(wanted))
        return f"Downloading {len(selected_indices)} file(s), {config.human_size(wanted)} selected."

    def discard(self, info_hash: str) -> None:
        self._remove(info_hash, delete_files=True)

    def _remove(self, info_hash: str, delete_files: bool) -> None:
        with self._lock:
            handle = self._handles.pop(info_hash, None)
            name = self._names.pop(info_hash, None)
            if handle is not None and self._session is not None:
                try:
                    if delete_files:
                        self._session.remove_torrent(handle, lt.options_t.delete_files)
                    else:
                        self._session.remove_torrent(handle)
                except Exception as exc:
                    log.warning("remove_torrent failed: %s", exc)
            if delete_files:
                # libtorrent's delete_files can leave partial files behind (async
                # removal, file locks); sweep the torrent's own directory after
                # it has unwound. Only the torrent's named dir + partfiles touch.
                threading.Thread(target=self._sweep_files,
                                 args=(name, info_hash), daemon=True).start()

    @staticmethod
    def _sweep_files(name: str | None, info_hash: str) -> None:
        import shutil
        time.sleep(2.0)
        candidates = [config.DOWNLOAD_DIR / f".{info_hash}.parts"]
        if name:
            safe = name.replace("\\", "/").split("/")[0]
            candidates.append(config.DOWNLOAD_DIR / safe)
        for path in candidates:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                log.info("swept leftover files: %s", path)

    # ----------------------------------------------------------------- status
    def snapshots(self) -> list[TorrentSnapshot]:
        out = []
        with self._lock:
            handles = list(self._handles.items())
        for ih, handle in handles:
            try:
                status = handle.status()
            except Exception:
                continue
            total = status.total_wanted
            done = status.total_wanted_done
            rate = status.download_payload_rate
            eta = None
            if rate > 0 and total > done:
                eta = (total - done) / rate
            name = self._names.get(ih)
            if not name:
                st_name = getattr(status, "name", "") or ""
                name = st_name or ih
            out.append(TorrentSnapshot(
                info_hash=ih,
                name=name,
                state=_state_label(status),
                progress=(done / total) if total else 0.0,
                total_wanted=total,
                total_wanted_done=done,
                download_rate=rate,
                num_seeds=status.num_seeds,
                num_peers=status.num_peers,
                eta_sec=eta,
                files=self.files(ih),
            ))
        return out
