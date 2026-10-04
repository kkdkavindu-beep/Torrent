"""End-to-end media test: ffmpeg acquisition, probe, convert, upload staging.

Run: python tests/test_media.py
Downloads a static ffmpeg build on first run (~170 MB on Windows).
"""
import logging
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

from magnetar_colab import config, drive_manager, media_info, pipeline, transcoder  # noqa: E402


def make_test_media(ffmpeg: str, outdir: pathlib.Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    jobs = [
        # 1080p H.264 (needs downscale to 720p)
        (["-f", "lavfi", "-i", "testsrc2=duration=4:size=1920x1080:rate=24",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(outdir / "big_1080p.mp4")]),
        # 720p H.265 in mkv (already at target -> copy)
        (["-f", "lavfi", "-i", "testsrc2=duration=3:size=1280x720:rate=24",
          "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
          "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(outdir / "small_720p.mkv")]),
        # 480p (below target -> copy)
        (["-f", "lavfi", "-i", "testsrc2=duration=2:size=854x480:rate=24",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          str(outdir / "tiny_480p.mp4")]),
        # audio only
        (["-f", "lavfi", "-i", "sine=frequency=660:duration=3",
          "-c:a", "libmp3lame", str(outdir / "song.mp3")]),
    ]
    for args in jobs:
        proc = subprocess_run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + args)
        assert proc == 0, f"failed to generate {args[-1]}"


def subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def main() -> None:
    print("== ensure_ffmpeg (downloads build if missing) ==")
    ffmpeg, ffprobe = media_info.ensure_ffmpeg()
    print("ffmpeg:", ffmpeg)
    print("ffprobe:", ffprobe)

    ws = config.DOWNLOAD_DIR / "TestTorrent"
    if not (ws / "big_1080p.mp4").is_file():
        print("== generating test media ==")
        make_test_media(ffmpeg, ws)

    print("== probe ==")
    infos = media_info.probe_many(sorted(ws.iterdir()), ffprobe)
    by_name = {i.name: i for i in infos}
    big = by_name["big_1080p.mp4"]
    assert big.media_type == "video" and big.vcodec == "h264", (big.media_type, big.vcodec)
    assert big.height == 1080 and big.resolution == "1080p", (big.height, big.resolution)
    assert big.audio_summary != "-", big.audio_summary
    assert big.duration_sec and 3.5 < big.duration_sec < 4.5, big.duration_sec
    small = by_name["small_720p.mkv"]
    assert small.vcodec == "hevc" and small.container == "mkv" and small.height == 720
    assert small.resolution == "720p"
    tiny = by_name["tiny_480p.mp4"]
    assert tiny.height == 480 and tiny.resolution == "480p"
    song = by_name["song.mp3"]
    assert song.media_type == "audio" and song.audio_summary != "-"
    for i in infos:
        print(f"  {i.name:20s} {i.media_type:6s} {i.vcodec_label or '-':12s} "
              f"{i.resolution:6s} {i.audio_summary}")

    print("== convert 1080p -> 720p (CPU rung expected here) ==")
    gpu = transcoder.setup_gpu()
    print(gpu.describe())
    out = config.CONVERTED_DIR / "big_1080p.converted.mp4"
    seen = []
    result = transcoder.convert(ws / "big_1080p.mp4", out, 720,
                                info=big, gpu=gpu,
                                progress_cb=lambda pct, speed: seen.append(pct))
    assert result.is_file() and result.stat().st_size > 0
    assert seen and seen[-1] == 1.0, f"progress never completed: {seen[-5:]}"
    conv = media_info.probe(result, ffprobe)
    assert conv.height == 720, conv.height
    assert conv.width == 1280, f"aspect ratio broken: {conv.width}x{conv.height}"
    assert conv.vcodec in ("h264", "hevc"), conv.vcodec
    assert abs((conv.duration_sec or 0) - (big.duration_sec or 0)) < 2.0
    print(f"  converted: {result.name} {conv.width}x{conv.height} "
          f"{config.human_size(result.stat().st_size)}")

    print("== drive staging upload + skip-if-exists ==")
    dest_dir = config.drive_base("TorrentColab") / "TestTorrent"
    (dest_dir / result.name).unlink(missing_ok=True)  # clean slate from prior runs
    status1 = drive_manager.upload_file(result, dest_dir)
    status2 = drive_manager.upload_file(result, dest_dir)
    assert status1 == "uploaded" and status2.startswith("skipped"), (status1, status2)
    on_drive = dest_dir / result.name
    assert on_drive.stat().st_size == result.stat().st_size
    print(f"  {status1} then {status2}")

    print("== pipeline scan ==")
    pipe = pipeline.Pipeline()
    rows, selected, _ = pipe.scan("720p")
    assert rows, "no plan rows"
    actions = {r.name: (r.action, r.action_label) for r in rows}
    print("  plan:", actions)
    assert actions["big_1080p.mp4"][0] == "convert"
    assert actions["small_720p.mkv"][0] == "copy"
    assert actions["tiny_480p.mp4"][0] == "copy"
    assert actions["song.mp3"][0] == "copy"
    assert len(selected) == 4  # videos + audio preselected
    print("\nALL MEDIA TESTS PASSED")


if __name__ == "__main__":
    main()
