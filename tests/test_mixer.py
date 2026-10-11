"""Mix studio: tracklist, ffmpeg command, real rendering (when ffmpeg is installed), web flow."""
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import mixer
from app.main import app
from app.video import probe

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


def test_track_name_from_tags_or_file_name():
    assert mixer.track_name("x.mp3", {"artist": "Kygo", "title": "Firestone"}) == "Kygo - Firestone"
    assert mixer.track_name("01 - Artist - Song.mp3", {}) == "Artist - Song"
    assert mixer.track_name("02_Deep_Sunset.wav", {}) == "Deep Sunset"
    assert mixer.track_name("2 Unlimited - No Limit.mp3", {}) == "2 Unlimited - No Limit"


def test_tracklist_with_crossfade_and_hours():
    tracks = [{"name": "A", "duration": 1800.0}, {"name": "B", "duration": 1800.0}, {"name": "C", "duration": 600.0}]
    text, total = mixer.build_tracklist(tracks, 0)
    assert text.splitlines() == ["0:00:00 A", "0:30:00 B", "1:00:00 C"]
    assert total == 4200
    text, total = mixer.build_tracklist(tracks, 10)  # each next track starts 10 s earlier
    assert text.splitlines()[1:] == ["0:29:50 B", "0:59:40 C"]
    assert total == 4180
    short, _ = mixer.build_tracklist([{"name": "A", "duration": 125.0}], 5)
    assert short == "00:00 A"


def test_crossfade_is_limited_by_shortest_track():
    assert mixer.effective_crossfade([100, 4], 10) == 2
    assert mixer.effective_crossfade([100], 10) == 0


def test_ffmpeg_command_loops_clip_and_joins_tracks(tmp_path):
    tracks = [{"path": "a.mp3", "duration": 10}, {"path": "b.mp3", "duration": 10}]
    cmd = mixer.ffmpeg_command(tmp_path / "clip.mp4", tracks, 3, 17, tmp_path / "out.mp4")
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "acrossfade=d=3.000[x1]" in graph and cmd[cmd.index("-t") + 1] == "17.000"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    no_fade = mixer.ffmpeg_command(tmp_path / "clip.mp4", tracks, 0, 20, tmp_path / "out.mp4")
    assert "concat=n=2:v=0:a=1" in no_fade[no_fade.index("-filter_complex") + 1]
    one_mp3 = mixer.ffmpeg_command(tmp_path / "clip.mp4", [{"path": "mix.mp3", "duration": 3600, "codec": "mp3"}],
                                   3, 3600, tmp_path / "out.mp4")
    assert "-filter_complex" not in one_mp3 and one_mp3[one_mp3.index("-c:a") + 1] == "copy"


def test_parse_progress():
    assert mixer.parse_progress("out_time_us=5000000\n", 10) == 50
    assert mixer.parse_progress("out_time_ms=20000000", 10) == 99  # never 100 before ffmpeg ends
    assert mixer.parse_progress("out_time_us=N/A", 10) is None
    assert mixer.parse_progress("frame=10", 10) is None


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)


@needs_ffmpeg
@pytest.mark.parametrize("clip_name", ["clip.mp4", "clip.webm"])
def test_render_real_mix(tmp_path, monkeypatch, clip_name):
    monkeypatch.setattr(mixer.config, "MIXES_DIR", tmp_path)
    folder = mixer.new_folder()
    clip = folder / clip_name
    codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if clip_name.endswith(".mp4") else ["-c:v", "libvpx-vp9"]
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1", *codec, clip)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-metadata", "artist=Kygo",
           "-metadata", "title=Firestone", folder / "01.mp3")
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:duration=2", "-ac", "1", "-ar", "22050", folder / "02.wav")
    mix = mixer.create_mix("Summer mix", clip, [("01.mp3", folder / "01.mp3"), ("02 - Sunset.wav", folder / "02.wav")], 1)
    assert mix.tracklist == "00:00 Kygo - Firestone\n00:02 Sunset"

    assert mixer.MixWorker().render(mix.id) == "done"
    mix = mixer.get_mix(mix.id)
    assert mix.status == "done" and mix.progress == 100
    info = probe(mixer.Path(mix.video_path))
    assert abs(info.duration - 4) < 0.5  # 3 s + 2 s - 1 s crossfade, the 1-second clip looped

    single = mixer.create_mix("", clip, [("01.mp3", folder / "01.mp3")], 0)  # one mp3: copied as is
    assert mixer.MixWorker().render(single.id) == "done"
    assert abs(probe(mixer.Path(mixer.get_mix(single.id).video_path)).duration - 3) < 0.5


@needs_ffmpeg
def test_web_mix_flow(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.init_db", lambda url=None: None)
    monkeypatch.setattr(mixer.config, "MIXES_DIR", tmp_path / "mixes")
    monkeypatch.setattr(mixer.worker, "wake", lambda: None)  # render in the test, not in the thread
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1", "-pix_fmt", "yuv420p", tmp_path / "c.mp4")
    ffmpeg("-f", "lavfi", "-i", "sine=duration=2", tmp_path / "t.mp3")
    with TestClient(app) as c:
        r = c.post("/mix", data={"title": "x"}, follow_redirects=False)
        assert r.headers["location"].endswith("mix.err_tracks")
        files = [("clip", ("c.mp4", (tmp_path / "c.mp4").read_bytes(), "video/mp4")),
                 ("tracks", ("b.mp3", (tmp_path / "t.mp3").read_bytes(), "audio/mpeg")),
                 ("tracks", ("a.mp3", (tmp_path / "t.mp3").read_bytes(), "audio/mpeg"))]
        r = c.post("/mix", data={"title": "Deep", "crossfade": "0"}, files=files, follow_redirects=False)
        assert r.headers["location"] == "/mix"
        mix = mixer.all_mixes()[0]
        assert mix.tracklist == "00:00 a\n00:02 b"  # sorted by file name
        assert "Deep" in c.get("/mix").text
        mixer.MixWorker().render(mix.id)
        r = c.post(f"/mixes/{mix.id}/post")
        target = r.headers["HX-Redirect"]
        assert target.startswith("/?video=") and target.endswith(f"&mix={mix.id}")
        page = c.get(target).text
        assert "00:00 a" in page and 'value="Deep"' in page
        assert c.post(f"/mixes/{mix.id}/delete").status_code == 200
        assert mixer.get_mix(mix.id) is None
