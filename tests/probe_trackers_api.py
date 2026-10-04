import libtorrent as lt

print("handle.add_tracker:", "add_tracker" in dir(lt.torrent_handle))
d = lt.default_settings()
for key in ("announce_to_all_trackers", "announce_to_all_tiers", "connections_limit",
            "dht_bootstrap_nodes", "active_downloads", "aio_threads"):
    print(f"settings[{key!r}]:", key in d)
