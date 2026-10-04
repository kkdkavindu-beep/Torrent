"""Launch the Gradio UI locally (no share) and hit it over HTTP."""
import json
import pathlib
import sys
import time
import urllib.request
from collections import deque

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from magnetar_colab import app, config, pipeline  # noqa: E402
from magnetar_colab.torrent_client import TorrentClient  # noqa: E402

PORT = 7861


def fetch(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read()


def main() -> None:
    ring: deque = deque(maxlen=400)
    app._setup_logging(ring)
    client = TorrentClient()
    client.start()
    pipe = pipeline.Pipeline()
    demo = app.build_ui(client, pipe, ring)
    demo.launch(prevent_thread_lock=True, share=False, quiet=True, server_port=PORT,
                show_error=True)
    time.sleep(3)

    status, body = fetch(f"http://127.0.0.1:{PORT}/")
    assert status == 200, status
    assert b"gradio" in body.lower(), "not a gradio page"

    status, body = fetch(f"http://127.0.0.1:{PORT}/config")
    assert status == 200
    cfg = json.loads(body)
    component_types = [c.get("type") for c in cfg.get("components", [])]
    print("components:", {t: component_types.count(t) for t in set(component_types)})
    for expected in ("textbox", "checkboxgroup", "dropdown", "dataframe", "timer", "button"):
        assert expected in component_types, f"missing component: {expected}"

    # keep it up briefly to let the Timer tick fire without exceptions
    time.sleep(4)
    print("\nUI LOG TAIL:")
    for line in list(ring)[-8:]:
        print(" ", line)
    demo.close()
    client.shutdown()
    print("UI TEST PASSED")


if __name__ == "__main__":
    main()
