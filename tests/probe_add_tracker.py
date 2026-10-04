import libtorrent as lt
import inspect

print("has announce_entry:", hasattr(lt, "announce_entry"))
h_doc = lt.torrent_handle.add_tracker.__doc__
print("add_tracker doc:", (h_doc or "")[:300])
ae = lt.announce_entry("udp://example.com:1337/announce")
print("announce_entry constructed ok:", ae.url if hasattr(ae, "url") else "?")
