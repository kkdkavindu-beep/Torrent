# 🧲 Magnetar Colab

Torrent downloader, video converter and Google Drive uploader that runs entirely
in a free Google Colab notebook with a Gradio web UI. All the logic lives in this
repo — the notebook is just a 4-cell bootstrap that mounts Drive, clones this
repo and launches the UI.

Companion to the [Magnetar](https://github.com/kkdkavindu-beep) Android torrent
app (the libtorrent patterns here are ported from it).

## Quick start

1. Download [`colab_launcher.ipynb`](colab_launcher.ipynb) and upload it anywhere
   in **your Google Drive** (once — it pulls the latest code from this repo on
   every run).
2. Double-click it in Drive → **Open with Google Colab**.
3. Set the runtime to **T4 GPU** if you want hardware-accelerated conversion
   (*Runtime → Change runtime type → T4 GPU*). Everything also works on CPU-only
   runtimes; conversion is just slower.
4. *Runtime → Run all*. You'll get the Google Drive permission prompt once per
   session, then a public `gradio.live` link — open it.

## Workflow

**Tab 1 · Download** — paste a magnet link → *Fetch file list* (metadata only,
nothing is downloaded yet). Torrents with folders get a **folder checklist**
(one click selects/deselects everything inside) plus a *Browse inside a folder*
dropdown for picking individual files, with "all/clear view" buttons and a live
"Selected x/y files" summary. Hit *Start download*; progress (per file, speed,
peers, ETA) auto-refreshes every second.

**Tab 2 · Videos → Drive** — *Scan downloads* probes every media file with
ffprobe and shows container / codec / resolution / audio. Choose a target
(e.g. *downscale videos larger than 720p*) — anything taller than the target is
re-encoded with the aspect ratio preserved (`scale=-2:H`, never upscaled,
never cropped); everything else is copied byte-for-byte. Hit
*Convert & upload* and watch per-file progress. Files land in
`MyDrive/<folder>/<torrent-name>/`.

**Tab 3 · Status** — GPU/NVENC capability report, disk space and a live log.

## Conversion details

| Input | Height vs target | What happens |
|---|---|---|
| any | smaller than target | straight copy, no re-encode |
| any | larger than target | downscale + re-encode (NVENC on GPU, libx264 on CPU) |
| audio/subtitle files | — | copied as-is |

Encoder ladder on a T4 runtime: **NVDEC → scale_cuda → NVENC** (full GPU) →
**NVENC** (CPU decode) → **libx264/x265** (CPU). Colab's stock ffmpeg has no
NVENC, so a static build is fetched once and cached on Drive
(`MyDrive/.cache/magnetar_ffmpeg`), surviving session restarts.

Every converted file is re-probed (stream count + duration) before anything is
deleted from the workspace.

## Settings

| Setting | Where | Default |
|---|---|---|
| Target resolution | Tab 2 dropdown | 720p |
| Encoder for conversions (H.264 / H.265) | Tab 2 | H.264 |
| Audio (copy / AAC) | Tab 2 | Copy — auto-AAC only when the codec isn't MP4-safe |
| Container for conversions | Tab 2 | Keep (legacy containers like AVI/WMV/TS become MP4) |
| Drive destination folder | Tab 2 | `TorrentColab` |
| Delete local after upload | Tab 2 | off |
| Gradio password | set `MAGNETAR_PASSWORD` env var in the notebook | off (share links are random URLs) |

## Notes & limitations

- Colab VMs are NATed (no inbound ports), but outbound-only uTP/DHT works fine
  for most public torrents; magnets are augmented with a tracker list to
  improve peer discovery.
- Files already on Drive with the same name **and** size are skipped, so
  re-running a job is safe.
- Free Colab sessions idle out after ~90 min and hard-stop at ~12 h; long
  downloads won't survive a disconnect. Finish, upload, then close.
- If a torrent shows no metadata after 45 s it's probably dead — try another
  source.

## Repo layout

```
magnetar_colab/
├── config.py          paths, trackers, presets
├── magnets.py         magnet URI parsing + tracker augmentation (port of Magnets.kt)
├── torrent_client.py  libtorrent session, metadata probe, file priorities, progress
├── media_info.py      ffprobe inspection + static ffmpeg acquisition
├── transcoder.py      GPU/CPU conversion ladder + progress parsing + output verification
├── drive_manager.py   chunked Drive upload, skip-if-exists, cleanup
├── pipeline.py        scan → plan → convert+upload orchestration
└── app.py             Gradio UI
```
