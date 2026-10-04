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


if __name__ == '__main__':
    test_magnets()
