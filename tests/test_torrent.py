"""Live libtorrent test: probe metadata -> pause -> select -> brief download -> discard.

Uses Big Buck Bunny (Blender Foundation, free/legal torrent).
Run: python tests/test_torrent.py   (needs internet; ~60s)
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

    print("== probing metadata (45s max) ==")
    result = client.probe_metadata(BBB, progress_cb=lambda p, peers: None)
    assert result.files, "no files in metadata"
    print(f"  name: {result.name}")
    print(f"  peers: {result.peers}, total: {config.human_size(result.total_size)}")
    for entry in result.files:
        print(f"    [{entry.index}] {entry.path} ({config.human_size(entry.size)})")

    # torrent must be paused after probe (no payload downloaded yet).
    # libtorrent races piece requests against priority application on magnets,
    # so a tiny amount of early bytes (<5%) can slip in before the pause.
    time.sleep(2)
    snap = client.snapshots()[0]
    assert snap.state == "PAUSED", f"expected PAUSED after probe, got {snap.state}"
    assert snap.total_wanted_done == 0, "wanted payload downloaded during probe"
    done_before = sum(f.progress for f in snap.files)
    assert done_before < 0.05, f"payload leaked during probe: {done_before}"

    print("== selecting the largest file and downloading briefly ==")
    biggest = max(result.files, key=lambda f: f.size)
    client.start_download(result.info_hash, {biggest.index})
    time.sleep(12)
    snap = client.snapshots()[0]
    print(f"  state={snap.state} progress={snap.progress*100:.2f}% "
          f"rate={config.human_size(snap.download_rate)}/s "
          f"peers={snap.num_peers} seeds={snap.num_seeds}")
    assert snap.state in ("DOWNLOADING", "STARTING", "CHECKING", "FINISHED", "SEEDING"), snap.state

    print("== discard (remove + delete files) ==")
    client.discard(result.info_hash)
    time.sleep(4)  # sweep thread deletes leftovers 2s after removal
    assert not client.snapshots(), "torrent still in session after discard"
    leftover = [p for p in config.DOWNLOAD_DIR.rglob("*") if "Bunny" in p.name or ".parts" in p.name]
    assert not leftover, f"files left after discard: {leftover}"
    client.shutdown()
    print("TORRENT TEST PASSED")


if __name__ == "__main__":
    main()
