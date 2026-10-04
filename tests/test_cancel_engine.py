"""Cancel flow at engine level: start a download mid-way, cancel, verify sweep.

This is exactly what the UI's 'Cancel & delete files' button calls.
Run: python tests/test_cancel_engine.py   (needs internet; ~30s)
"""
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

from magnetar_colab import config  # noqa: E402
from magnetar_colab.torrent_client import TorrentClient  # noqa: E402

BBB = ("magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
       "&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce")


def main() -> None:
    client = TorrentClient()
    client.start()

    print("== probe + start the big file ==")
    result = client.probe_metadata(BBB, progress_cb=lambda p, peers: None)
    mp4 = max(result.files, key=lambda f: f.size)
    client.start_download(result.info_hash, {mp4.index})
    time.sleep(8)
    snap = client.snapshots()[0]
    print(f"  downloading: state={snap.state} progress={snap.progress * 100:.1f}%")
    assert snap.progress > 0, "no download progress - cannot test a real midway cancel"
    partial = list(config.DOWNLOAD_DIR.rglob("*.mp4")) + list(config.DOWNLOAD_DIR.rglob("*.mp4.part"))
    assert partial, "expected partial payload on disk before cancel"

    print("== cancel (remove + delete files) ==")
    client.discard(result.info_hash)  # <- what the Cancel button calls
    time.sleep(4)                     # sweep thread runs 2s after removal
    assert not client.snapshots(), "torrent still in session after cancel"
    leftover = [p for p in config.DOWNLOAD_DIR.rglob("*")
                if "Bunny" in p.name or ".parts" in p.name]
    assert not leftover, f"partial files left after cancel: {leftover}"
    client.shutdown()
    print("CANCEL ENGINE TEST PASSED")


if __name__ == "__main__":
    main()
