"""Quick functional tests runnable locally (and useful on Colab too)."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from magnetar_colab import magnets  # noqa: E402


def test_magnets():
    m = ('magnet:?xt=urn:btih:DD8255ECDC7CA55FB0BBF81323D87062DB1F6D1C'
         '&dn=Big+Buck+Bunny&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce')
    assert magnets.is_magnet(m)
    ih = magnets.info_hash(m)
    assert ih == 'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c', ih
    assert magnets.display_name(m) == 'Big Buck Bunny', magnets.display_name(m)
    assert 'tracker.opentrackr.org' in magnets.trackers(m)[0]

    aug = magnets.augment(m)
    assert aug.startswith(m)
    tr2 = magnets.trackers(aug)
    assert len(tr2) == len(set(t.lower() for t in tr2)), 'duplicate trackers after augment'
    assert aug.lower().count('urn:btih:dd8255') == 1
    assert magnets.augment(aug) == aug, 'augment not idempotent'
    assert magnets.augment('http://x.com') == 'http://x.com'

    b32 = 'magnet:?xt=urn:btih:MTVVJDTMB4SZQ4RK7SRQVUVBWHVQ2W6H&dn=test'
    h = magnets.info_hash(b32)
    assert h and len(h) == 40, h
    print('magnets OK:', ih[:16] + '...', f'{len(tr2)} trackers after augment')


def test_picker_logic():
    from magnetar_colab import app
    from magnetar_colab.torrent_client import TorrentFileEntry

    files = [
        TorrentFileEntry(index=0, path="Movie/Movie.1080p.mkv", size=1000),
        TorrentFileEntry(index=1, path="Movie/sample.avi", size=100),
        TorrentFileEntry(index=2, path="Movie/Subs/en.srt", size=10),
        TorrentFileEntry(index=3, path="poster.jpg", size=5),
    ]
    data = app._build_picker_data(files)
    assert set(data["folders"].keys()) == {"Movie", "Movie/Subs", ""}, data["folders"].keys()
    assert data["folders"]["Movie"] == [0, 1]
    assert data["folders"][""] == [3]
    assert data["order"] == ["", "Movie", "Movie/Subs"], data["order"]
    assert data["total_size"] == 1115

    everything = set(range(4))
    # unchecking all folders empties the selection
    assert app._apply_folder_check([], data, everything) == set()
    # checking just the Movie folder selects its files only
    sel = app._apply_folder_check(["Movie"], data, set())
    assert sel == {0, 1}
    # per-file override inside a view then folder re-check restores the folder
    sel = app._apply_view_check("Movie", ["1"], data, everything)
    assert sel == {1, 2, 3}
    sel = app._apply_view_check("Movie", [], data, everything)
    assert sel == {2, 3}
    # folder checkboxes reflect all-selected state
    assert app._folder_values(data, everything) == ["", "Movie", "Movie/Subs"]
    assert app._folder_values(data, {0, 1}) == ["Movie"]
    # summary text
    text = app._summary_text(data, {0, 3})
    assert "2/4" in text and "1005 B" in text, text
    print("picker logic OK")


def test_trackers():
    from magnetar_colab import config, trackers
    text = "# comment\n\nudp://good.example.com:1337/announce\nhttp://x.example.org/announce\nnot a url\n"
    parsed = trackers.parse_tracker_list(text)
    assert parsed == ["udp://good.example.com:1337/announce", "http://x.example.org/announce"]
    assert len(config.DEFAULT_TRACKERS) >= 20, "fallback list too thin"
    assert any(t.startswith("wss://") for t in config.DEFAULT_TRACKERS)
    assert config.DHT_BOOTSTRAP_NODES.count(":") >= 4
    print(f"trackers OK: {len(config.DEFAULT_TRACKERS)} embedded fallback trackers")


if __name__ == '__main__':
    test_magnets()
    test_picker_logic()
    test_trackers()
