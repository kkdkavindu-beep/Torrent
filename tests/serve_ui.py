"""Serve the Gradio UI locally for a visual check: python tests/serve_ui.py [port]"""
import pathlib
import sys
import time
from collections import deque

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from magnetar_colab import app, pipeline  # noqa: E402
from magnetar_colab.torrent_client import TorrentClient  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 7862

ring: deque = deque(maxlen=400)
app._setup_logging(ring)
client = TorrentClient()
client.start()
pipe = pipeline.Pipeline()
demo = app.build_ui(client, pipe, ring)
demo.launch(prevent_thread_lock=True, share=False, quiet=True, server_port=port)
print(f"UI serving on http://127.0.0.1:{port}", flush=True)
time.sleep(900)
