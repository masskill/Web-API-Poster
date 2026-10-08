import shutil
import subprocess
from pathlib import Path

import pytest

from app.platforms import load_platforms
from app.video import VideoInfo, check_video, parse_probe, probe


def codes(warnings):
    return {w["code"] for w in warnings}


def test_vertical_short_video_fits_youtube_and_telegram():
    info = VideoInfo(duration=30, width=1080, height=1920, size_mb=20, ext="mp4")
    p = load_platforms()
    assert check_video(info, "youtube", p["youtube"]) == []
    assert check_video(info, "telegram", p["telegram"]) == []


def test_limits_produce_warnings():
    info = VideoInfo(duration=200, width=1000, height=1000, size_mb=80, ext="avi")
    w = check_video(info, "telegram", load_platforms()["telegram"])
    assert codes(w) == {"size", "format"}
    w = check_video(info, "x", load_platforms()["x"])
    assert codes(w) == {"duration", "format"}
    w = check_video(VideoInfo(10, 1920, 1080, 1, "mp4"), "tiktok", load_platforms()["tiktok"])
    assert codes(w) == {"aspect"}


def test_parse_probe_handles_rotation(tmp_path):
    data = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                     "side_data_list": [{"rotation": -90}]}],
        "format": {"duration": "12.5", "size": str(5 * 1024 * 1024)},
    }
    info = parse_probe(data, Path("clip.MP4"))
    assert (info.width, info.height, info.duration, info.size_mb, info.ext) == (1080, 1920, 12.5, 5.0, "mp4")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_probe_real_file(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x568:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    info = probe(video)
    assert (info.width, info.height) == (320, 568)
    assert 1.5 < info.duration < 2.5
