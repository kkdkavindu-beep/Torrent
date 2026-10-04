"""Gradio UI: magnet download -> media scan -> convert -> upload to Drive."""
from __future__ import annotations

import logging
import os
import time
from collections import deque

import gradio as gr

from . import config, pipeline, trackers, transcoder
from .torrent_client import HAVE_LIBTORRENT, TorrentClient

log = logging.getLogger(config.LOG)

DL_HEADERS = ["Item", "State", "Progress", "Size", "Speed", "Seeds/Peers", "ETA"]
PLAN_HEADERS = ["File", "Type", "Container", "Video", "Resolution", "Audio", "Size", "Action"]
JOB_HEADERS = ["File", "Stage", "Progress", "Transferred", "Size", "Details"]

ALL_VIEW = "@all"


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


# ------------------------------------------------------- file picker internals
def _build_picker_data(files) -> dict:
    """Group probed torrent files by containing folder for the file picker."""
    paths, sizes, folders = {}, {}, {}
    for entry in files:
        rel = entry.path.replace("\\", "/").lstrip("/")
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        paths[entry.index] = rel
        sizes[entry.index] = entry.size
        folders.setdefault(parent, []).append(entry.index)
    order = sorted(folders, key=lambda d: (d != "", d.lower()))
    return {
        "paths": paths,
        "sizes": sizes,
        "folders": folders,          # dir path ("" = root) -> [indices]
        "order": order,
        "total_size": sum(sizes.values()),
    }


def _folder_label(data: dict, folder: str) -> str:
    indices = data["folders"][folder]
    size = sum(data["sizes"][i] for i in indices)
    name = folder or "(root files)"
    return f"📁 {name} — {len(indices)} file(s), {config.human_size(size)}"


def _summary_text(data: dict, selection: set) -> str:
    picked = sum(data["sizes"][i] for i in selection)
    return (f"**Selected {len(selection)}/{len(data['paths'])} files — "
            f"{config.human_size(picked)} of {config.human_size(data['total_size'])}**")


def _folder_values(data: dict, selection: set) -> list[str]:
    """Folder checkboxes reflect reality: checked iff every file inside is selected."""
    return [d for d in data["order"]
            if all(i in selection for i in data["folders"][d])]


def _files_view_update(data: dict, selection: set, view: str):
    if not data:
        return gr.update()
    if view == ALL_VIEW:
        choices = [(f"{data['paths'][i]}  ({config.human_size(data['sizes'][i])})", str(i))
                   for i in sorted(data["paths"])]
    else:
        choices = [(f"{data['paths'][i].rsplit('/', 1)[-1]}"
                    f"  ({config.human_size(data['sizes'][i])})", str(i))
                   for i in data["folders"].get(view, [])]
    value = [v for _, v in choices if int(v) in selection]
    return gr.update(choices=choices, value=value)


def _apply_folder_check(checked_folders, data: dict, selection: set) -> set:
    """Folder checkbox semantics are absolute: checked -> all its files selected."""
    checked = set(checked_folders or [])
    sel = set(selection or set())
    for folder, indices in data["folders"].items():
        if folder in checked:
            sel.update(indices)
        else:
            sel.difference_update(indices)
    return sel


def _apply_view_check(view: str, checked, data: dict, selection: set) -> set:
    """Replace the selection state of the files currently in view."""
    sel = set(selection or set())
    view_indices = set(range(len(data["paths"]))) if view == ALL_VIEW \
        else set(data["folders"].get(view, []))
    sel -= view_indices
    sel |= {int(v) for v in (checked or [])}
    return sel


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

        data = _build_picker_data(result.files)
        data["info_hash"] = result.info_hash
        selection = set(data["paths"].keys())  # default: everything

        # land the user in the biggest folder (unless the torrent is all loose files)
        default_view = max(data["order"], key=lambda d: len(data["folders"][d])) \
            if data["order"] else ALL_VIEW
        browse_update = gr.update(
            choices=[("All files", ALL_VIEW)] + [(_folder_label(data, d), d) for d in data["order"]],
            value=default_view)
        folders_update = gr.update(choices=[(_folder_label(data, d), d) for d in data["order"]],
                                   value=list(data["order"]))
        label = (f"**{result.name}** - {len(result.files)} file(s), "
                 f"{config.human_size(result.total_size)} - {result.peers} peers connected.")
        return (label, browse_update, folders_update,
                _files_view_update(data, selection, default_view),
                data, selection, _summary_text(data, selection))

    def on_folders_change(checked_folders, browse, data, selection):
        if not data:
            raise gr.Error("Fetch the file list first.")
        sel = _apply_folder_check(checked_folders, data, selection)
        return sel, _summary_text(data, sel), _files_view_update(data, sel, browse)

    def on_files_change(checked, browse, data, selection):
        if not data:
            raise gr.Error("Fetch the file list first.")
        sel = _apply_view_check(browse, checked, data, selection)
        return sel, _summary_text(data, sel), gr.update(value=_folder_values(data, sel))

    def on_browse_change(browse, data, selection):
        if not data:
            return gr.update()
        return _files_view_update(data, selection, browse)

    def _force_view(browse, data, selection, want_all: bool):
        sel = _apply_view_check(browse, None, data, selection)
        if want_all:
            view_indices = set(range(len(data["paths"]))) if browse == ALL_VIEW \
                else set(data["folders"].get(browse, []))
            sel |= view_indices
        return (sel, _summary_text(data, sel), gr.update(value=_folder_values(data, sel)),
                _files_view_update(data, sel, browse))

    def download_handler(data, selection):
        if not data:
            raise gr.Error("Fetch the file list first.")
        if not selection:
            raise gr.Error("Nothing selected - check at least one folder or file.")
        try:
            return client.start_download(data["info_hash"], {int(i) for i in selection})
        except (ValueError, RuntimeError) as exc:
            raise gr.Error(str(exc)) from exc

    _cancel_choices_seen = {"key": None}

    def cancel_choices():
        snaps = client.snapshots()
        key = tuple(snap.info_hash for snap in snaps)
        if key == _cancel_choices_seen["key"]:
            return gr.update()  # unchanged -> leave the dropdown alone
        _cancel_choices_seen["key"] = key
        return gr.update(choices=[(f"{snap.name}  ({snap.info_hash[:8]}...)", snap.info_hash)
                                  for snap in snaps])

    def cancel_handler(info_hash: str):
        if not info_hash:
            raise gr.Error("Pick a torrent from the dropdown first.")
        name = ""
        for snap in client.snapshots():
            if snap.info_hash == info_hash:
                name = snap.name
                break
        client.discard(info_hash)  # stops the download and deletes its files
        msg = f"Cancelled '{name or info_hash[:8]}' and deleted its downloaded files."
        log.info(msg)
        return msg, gr.update(choices=[], value=None)

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
                probe_status = gr.Markdown("Paste a magnet link above, then fetch its file list.",
                                           container=False)
                folders_group = gr.CheckboxGroup(label="Folders — check what you want",
                                                 choices=[], value=[])
                with gr.Row():
                    browse_dd = gr.Dropdown(label="Browse inside a folder for individual files",
                                            choices=[], scale=3)
                    with gr.Column(scale=1):
                        with gr.Row():
                            pick_all_btn = gr.Button("✓ all in view", size="sm")
                            clear_view_btn = gr.Button("✗ clear view", size="sm")
                files_group = gr.CheckboxGroup(label="Files in view", choices=[], value=[])
                sel_summary = gr.Markdown("Fetch a file list to begin.", container=False)
                dl_btn = gr.Button("2️⃣ Start download", variant="primary")
                dl_status = gr.Markdown(container=False)
                with gr.Row():
                    cancel_dd = gr.Dropdown(label="Active torrent",
                                            choices=[], scale=3)
                    cancel_btn = gr.Button("🗑 Cancel & delete files", variant="stop", scale=1)
                cancel_status = gr.Markdown(container=False)
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

        data_state = gr.State(None)
        sel_state = gr.State(set())

        probe_btn.click(probe_handler, inputs=[magnet_box],
                        outputs=[probe_status, browse_dd, folders_group, files_group,
                                 data_state, sel_state, sel_summary])
        folders_group.change(on_folders_change,
                             inputs=[folders_group, browse_dd, data_state, sel_state],
                             outputs=[sel_state, sel_summary, files_group])
        browse_dd.change(on_browse_change,
                         inputs=[browse_dd, data_state, sel_state],
                         outputs=[files_group])
        files_group.change(on_files_change,
                           inputs=[files_group, browse_dd, data_state, sel_state],
                           outputs=[sel_state, sel_summary, folders_group])
        pick_all_btn.click(lambda b, d, s: _force_view(b, d, s, True),
                           inputs=[browse_dd, data_state, sel_state],
                           outputs=[sel_state, sel_summary, folders_group, files_group])
        clear_view_btn.click(lambda b, d, s: _force_view(b, d, s, False),
                             inputs=[browse_dd, data_state, sel_state],
                             outputs=[sel_state, sel_summary, folders_group, files_group])
        dl_btn.click(download_handler, inputs=[data_state, sel_state], outputs=[dl_status])
        cancel_btn.click(cancel_handler, inputs=[cancel_dd],
                         outputs=[cancel_status, cancel_dd])

        scan_btn.click(scan_handler, inputs=[target_res],
                       outputs=[plan_table, include_group, scan_summary])
        run_btn.click(run_handler,
                      inputs=[include_group, folder_tb, delete_cb, codec_dd, audio_dd, cont_dd],
                      outputs=[run_status])
        cancel_btn.click(pipe.cancel, outputs=[run_status])
        gpu_btn.click(gpu_handler, outputs=[gpu_md])

        timer = gr.Timer(1.0)
        timer.tick(lambda: (refresh_downloads(), refresh_jobs(), refresh_log(),
                            cancel_choices()),
                   outputs=[dl_table, jobs_table, log_tb, cancel_dd])
    return demo


def _keep_colab_alive() -> None:
    """Best-effort: fake a connect-button click on the Colab page every minute so
    free runtimes don't idle-disconnect while the Gradio UI is used from another
    tab. Harmless no-op where the selectors no longer match."""
    if not config.IS_COLAB:
        return
    try:
        from IPython.display import Javascript, display
        display(Javascript("""
        (function () {
          if (window.__magnetarKeepAlive) return;
          window.__magnetarKeepAlive = setInterval(function () {
            var btn = document.querySelector('colab-connect-button')
                   || document.querySelector('#connect')
                   || document.querySelector('button[aria-label*="onnect"]');
            if (btn) btn.click();
          }, 60000);
        })();"""))
        log.info("Colab keep-alive injected (connect click every 60s).")
    except Exception as exc:
        log.info("keep-alive not available: %s", exc)


def launch() -> None:
    """Entry point called by the notebook: prepare dirs, wire logging, start UI."""
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    config.CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    config.TORRENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

    ring: deque = deque(maxlen=400)
    _setup_logging(ring)

    client = TorrentClient()
    client.start()
    trackers.refresh_async(apply_cb=client.apply_trackers)
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

    _keep_colab_alive()
    try:
        demo.launch(
            share=True,
            show_error=True,
            inbrowser=False,
            auth=("user", password) if password else None,
        )
    except KeyboardInterrupt:
        log.info("UI stopped.")
    # never let the cell finish - a completed cell lets Colab idle-disconnect
    print("UI cell keeps running to hold the Colab session open. "
          "Interrupt again to release the runtime.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Runtime released - Colab may disconnect after the idle timeout.")
