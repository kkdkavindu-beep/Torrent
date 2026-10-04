"""Gradio UI: magnet download -> media scan -> convert -> upload to Drive."""
from __future__ import annotations

import logging
import os
import pathlib
from collections import deque

import gradio as gr

from . import config, pipeline, transcoder
from .torrent_client import HAVE_LIBTORRENT, TorrentClient

log = logging.getLogger(config.LOG)

DL_HEADERS = ["Item", "State", "Progress", "Size", "Speed", "Seeds/Peers", "ETA"]
PLAN_HEADERS = ["File", "Type", "Container", "Video", "Resolution", "Audio", "Size", "Action"]
JOB_HEADERS = ["File", "Stage", "Progress", "Transferred", "Size", "Details"]


class _RingHandler(logging.Handler):
    def __init__(self, ring: deque) -> None:
        super().__init__()
        self.ring = ring
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.append(self.format(record))
        except Exception:
            pass


def _setup_logging(ring: deque) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "urllib3", "gradio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger().addHandler(_RingHandler(ring))


def build_ui(client: TorrentClient, pipe: pipeline.Pipeline, log_ring: deque) -> gr.Blocks:
    # ------------------------------------------------------------------ tab 1
    def probe_handler(magnet: str, progress=gr.Progress()):
        if not HAVE_LIBTORRENT:
            raise gr.Error("libtorrent is not installed - re-run the notebook setup cell.")
        magnet = (magnet or "").strip()
        if not magnet:
            raise gr.Error("Paste a magnet link first.")

        def cb(pct: float, peers: int) -> None:
            progress(pct, desc=f"fetching metadata... {peers} peers")

        try:
            result = client.probe_metadata(magnet, progress_cb=cb)
        except (ValueError, TimeoutError) as exc:
            raise gr.Error(str(exc)) from exc

        choices = [(f"{entry.path}  ({config.human_size(entry.size)})", str(entry.index))
                   for entry in result.files]
        label = (f"**{result.name}** - {len(result.files)} file(s), "
                 f"{config.human_size(result.total_size)} - {result.peers} peers connected.")
        return (label, gr.update(choices=choices, value=[c[1] for c in choices]),
                {"info_hash": result.info_hash})

    def download_handler(selected, state):
        if not state or "info_hash" not in state:
            raise gr.Error("Fetch the file list first.")
        indices = {int(s) for s in (selected or [])}
        try:
            return client.start_download(state["info_hash"], indices)
        except (ValueError, RuntimeError) as exc:
            raise gr.Error(str(exc)) from exc

    def refresh_downloads():
        rows = []
        for snap in client.snapshots():
            eta = "-"
            if snap.eta_sec:
                eta = f"{int(snap.eta_sec // 60)}m {int(snap.eta_sec % 60)}s"
            rate = f"{config.human_size(snap.download_rate)}/s" if snap.download_rate else "-"
            rows.append([f"▶ {snap.name}", snap.state, f"{snap.progress * 100:.1f}%",
                         config.human_size(snap.total_wanted), rate,
                         f"{snap.num_seeds}S/{snap.num_peers}P", eta])
            if snap.state in ("DOWNLOADING", "STARTING") and 0 < len(snap.files) <= 40:
                for entry in snap.files:
                    if entry.priority == 0:
                        continue  # deselected by the user
                    rows.append([f"    ├ {entry.name}", "",
                                 f"{entry.progress * 100:.0f}%",
                                 config.human_size(entry.size), "", "", ""])
        return rows

    # ------------------------------------------------------------------ tab 2
    def scan_handler(target_label: str):
        try:
            rows, selected, _all = pipe.scan(target_label)
        except Exception as exc:
            raise gr.Error(f"Scan failed: {exc}") from exc
        plan_rows = [[r.name, r.media_type, r.container, r.vcodec_label, r.resolution,
                      r.audio_summary, config.human_size(r.size), r.action_label] for r in rows]
        choices = [(r.name, r.key) for r in rows]
        return (plan_rows, gr.update(choices=choices, value=selected), pipe.last_scan_summary)

    def run_handler(selected, folder, delete_after, codec_label, audio_label, container_label):
        if not selected:
            raise gr.Error("Nothing selected - run a scan and pick files.")
        return pipe.execute(
            list(selected),
            drive_folder=folder or config.DEFAULT_DRIVE_FOLDER,
            delete_after=bool(delete_after),
            vcodec_target=config.CODEC_TARGETS[codec_label],
            audio_mode=config.AUDIO_MODES[audio_label],
            container_mode=config.CONTAINER_MODES[container_label],
        )

    def refresh_jobs():
        return pipe.job_rows()

    # ------------------------------------------------------------------ tab 3
    def gpu_handler():
        return transcoder.setup_gpu(force_recheck=True).describe()

    def refresh_log():
        return "\n".join(list(log_ring)[-250:])

    # ------------------------------------------------------------------ layout
    with gr.Blocks(theme=gr.themes.Soft(), title="Magnetar Colab") as demo:
        gr.Markdown("# 🧲 Magnetar Colab\nTorrent downloader -> video converter -> Google Drive.")

        with gr.Tabs():
            with gr.Tab("⬇️ 1 · Download"):
                magnet_box = gr.Textbox(label="Magnet link", lines=3,
                                        placeholder="magnet:?xt=urn:btih:...")
                probe_btn = gr.Button("1️⃣ Fetch file list", variant="primary")
                probe_status = gr.Markdown("Paste a magnet link above, then fetch its file list.", container=False)
                file_group = gr.CheckboxGroup(label="Files - uncheck what you don't want",
                                              choices=[], value=[])
                dl_btn = gr.Button("2️⃣ Start download", variant="primary")
                dl_status = gr.Markdown(container=False)
                gr.Markdown("### Active downloads (auto-refresh)")
                dl_table = gr.Dataframe(headers=DL_HEADERS, value=[], interactive=False)

            with gr.Tab("🎬 2 · Videos → Drive"):
                with gr.Row():
                    scan_btn = gr.Button("🔍 Scan downloads", variant="primary")
                    scan_summary = gr.Markdown(container=False)
                plan_table = gr.Dataframe(headers=PLAN_HEADERS, value=[], interactive=False)
                include_group = gr.CheckboxGroup(label="Files to process", choices=[], value=[])
                with gr.Row():
                    target_res = gr.Dropdown([pipeline.NO_CONVERSION] + list(config.TARGET_HEIGHTS),
                                             value=config.DEFAULT_TARGET,
                                             label="Downscale videos larger than")
                    codec_dd = gr.Dropdown(list(config.CODEC_TARGETS),
                                           value=list(config.CODEC_TARGETS)[0],
                                           label="Encoder (for conversions)")
                    audio_dd = gr.Dropdown(list(config.AUDIO_MODES), value="Copy audio",
                                           label="Audio")
                    cont_dd = gr.Dropdown(list(config.CONTAINER_MODES),
                                          value="Keep original",
                                          label="Container (when converting)")
                with gr.Row():
                    folder_tb = gr.Textbox(value=config.DEFAULT_DRIVE_FOLDER,
                                           label="Drive folder (under MyDrive)")
                    delete_cb = gr.Checkbox(value=False,
                                            label="Delete local files after upload")
                run_btn = gr.Button("3️⃣ Convert & upload to Drive", variant="primary")
                run_status = gr.Markdown(container=False)
                jobs_table = gr.Dataframe(headers=JOB_HEADERS, value=[], interactive=False,
                                          label="Job progress (auto-refresh)")
                cancel_btn = gr.Button("Cancel jobs", variant="stop")

            with gr.Tab("🩺 3 · Status"):
                gpu_btn = gr.Button("Re-check GPU / NVENC")
                gpu_md = gr.Markdown(
                    "Click **Re-check GPU / NVENC** to probe the GPU. (Also runs "
                    "automatically the first time you convert something.)",
                    container=False)
                log_tb = gr.Textbox(label="Log", lines=20, interactive=False)

        probe_state = gr.State(None)
        probe_btn.click(probe_handler, inputs=[magnet_box],
                        outputs=[probe_status, file_group, probe_state])

        dl_btn.click(download_handler, inputs=[file_group, probe_state], outputs=[dl_status])
        scan_btn.click(scan_handler, inputs=[target_res],
                       outputs=[plan_table, include_group, scan_summary])
        run_btn.click(run_handler,
                      inputs=[include_group, folder_tb, delete_cb, codec_dd, audio_dd, cont_dd],
                      outputs=[run_status])
        cancel_btn.click(pipe.cancel, outputs=[run_status])
        gpu_btn.click(gpu_handler, outputs=[gpu_md])

        timer = gr.Timer(1.0)
        timer.tick(lambda: (refresh_downloads(), refresh_jobs(), refresh_log()),
                   outputs=[dl_table, jobs_table, log_tb])
    return demo


def launch() -> None:
    """Entry point called by the notebook: prepare dirs, wire logging, start UI."""
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    config.CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    config.TORRENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

    ring: deque = deque(maxlen=400)
    _setup_logging(ring)

    client = TorrentClient()
    client.start()
    pipe = pipeline.Pipeline()
    demo = build_ui(client, pipe, ring)

    password = os.environ.get("MAGNETAR_PASSWORD")
    print("=" * 62)
    print(f" Downloads    : {config.DOWNLOAD_DIR}")
    print(f" Drive target : {config.drive_base()}  (under MyDrive)")
    print(f" Workspace free: ", end="")
    free = config.disk_free(config.BASE_DIR)
    print(f"{config.human_size(free)}" if free else "unknown")
    print("=" * 62)

    demo.launch(
        share=True,
        show_error=True,
        inbrowser=False,
        auth=("user", password) if password else None,
    )
