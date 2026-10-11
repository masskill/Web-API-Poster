"""Mix studio: a short clip (e.g. a generated seamless loop) repeated under a long music mix.

Audio tracks are joined in file-name order, optionally with a smooth transition (crossfade),
and the description gets a YouTube tracklist ("00:00 Artist - Track", YouTube turns it into chapters).
The clip is copied, not re-encoded. One ready mix in mp3 / aac is copied too, so rendering takes seconds;
several tracks are joined and encoded to AAC (a few minutes per hour of music).
Uses ffmpeg (installed together with ffprobe).
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from sqlmodel import select

from app import config, posting
from app.db import get_session
from app.models import Mix, utcnow
from app.notify import message, notify
from app.video import probe

logger = logging.getLogger("poster.mixer")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
COPY_CODECS = {"h264", "hevc"}  # clips in these codecs are copied as is
COPY_CONTAINERS = {".mp4", ".mov", ".m4v", ".mkv"}
COPY_AUDIO = {"mp3", "aac"}  # a single track in these codecs goes into the mp4 unchanged


# ---- tracks and tracklist ----

def audio_info(path: Path) -> tuple[float, dict, str]:
    """(duration in seconds, lower-case tags, audio codec). Raises FileNotFoundError without ffprobe."""
    proc = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                           str(path)], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or "ffprobe failed")
    data = json.loads(proc.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise ValueError("no audio stream")
    fmt = data.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return float(fmt.get("duration") or 0), tags, stream.get("codec_name", "")


def track_name(filename: str, tags: dict) -> str:
    """'Artist - Title' from the file tags, else the file name without a leading number ('01 - ')."""
    if tags.get("artist") and tags.get("title"):
        return f"{tags['artist']} - {tags['title']}"
    stem = Path(filename).stem
    return re.sub(r"^\s*\d{1,3}\s*[-.)_]\s*", "", stem).replace("_", " ").strip() or stem


def effective_crossfade(durations: list[float], crossfade: float) -> float:
    """A transition cannot be longer than half of the shortest track."""
    if len(durations) < 2:
        return 0.0
    return max(0.0, min(crossfade, min(durations) / 2))


def fmt_time(seconds: float, long: bool) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if long else f"{s // 60:02d}:{s % 60:02d}"


def build_tracklist(tracks: list[dict], crossfade: float) -> tuple[str, float]:
    """(tracklist text, total duration). With a crossfade each next track starts that much earlier."""
    d = effective_crossfade([t["duration"] for t in tracks], crossfade)
    starts, t = [], 0.0
    for track in tracks:
        starts.append(t)
        t += track["duration"] - d
    total = t + d if tracks else 0.0
    long = total >= 3600
    return "\n".join(f"{fmt_time(s, long)} {tr['name']}" for s, tr in zip(starts, tracks)), total


# ---- ffmpeg ----

def ffmpeg_command(clip: Path, tracks: list[dict], crossfade: float, total: float, out: Path) -> list[str]:
    """The clip is looped endlessly (-stream_loop -1) and cut at the end of the music (-t)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(clip)]
    for track in tracks:
        cmd += ["-i", str(track["path"])]
    end = ["-t", f"{total:.3f}", "-c:v", "copy"]
    progress = ["-progress", "pipe:1", "-nostats", str(out)]
    if len(tracks) == 1 and tracks[0].get("codec") in COPY_AUDIO:  # one ready mix: no re-encoding, takes seconds
        return cmd + ["-map", "0:v:0", "-map", "1:a:0", *end, "-c:a", "copy", *progress]
    n, d = len(tracks), effective_crossfade([t["duration"] for t in tracks], crossfade)
    # Same sample rate and channels for every track, so they can be joined.
    parts = [f"[{i + 1}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]" for i in range(n)]
    last = "a0"
    if n > 1 and d > 0:
        for i in range(1, n):
            parts.append(f"[{last}][a{i}]acrossfade=d={d:.3f}[x{i}]")
            last = f"x{i}"
    elif n > 1:
        parts.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[joined]")
        last = "joined"
    return cmd + ["-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", f"[{last}]",
                  *end, "-c:a", "aac", "-b:a", "320k", *progress]


def prepare_clip(clip: Path, folder: Path) -> Path:
    """A clip that cannot be copied into mp4 (webm, gif...) is converted once to H.264; it is short, so this is quick."""
    info = probe(clip)
    if info.codec in COPY_CODECS and clip.suffix.lower() in COPY_CONTAINERS:
        return clip
    out = folder / "clip_h264.mp4"
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-an", "-c:v", "libx264",
                           "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(out)],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=1800)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip()[-500:] or "ffmpeg failed")
    return out


def parse_progress(line: str, total: float) -> int | None:
    """ffmpeg -progress prints 'out_time_us=12345678' (both _us and _ms are microseconds)."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and value.isdigit() and total > 0:
        return max(0, min(99, int(int(value) / 1_000_000 / total * 100)))
    return None


# ---- mixes ----

def new_folder() -> Path:
    folder = config.MIXES_DIR / uuid.uuid4().hex[:8]
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def create_mix(title: str, clip: Path, track_files: list[tuple[str, Path]], crossfade: float) -> Mix:
    """track_files: (original file name, saved path), already in the play order."""
    tracks = []
    for filename, path in track_files:
        duration, tags, codec = audio_info(path)
        tracks.append({"path": str(path), "name": track_name(filename, tags), "duration": round(duration, 3),
                       "codec": codec})
    tracklist, _ = build_tracklist(tracks, crossfade)
    with get_session() as s:
        mix = Mix(title=title.strip(), clip_path=str(clip), tracks=json.dumps(tracks, ensure_ascii=False),
                  crossfade=crossfade, tracklist=tracklist)
        s.add(mix)
        s.commit()
    return mix


def get_mix(mix_id: int) -> Mix | None:
    with get_session() as s:
        return s.get(Mix, mix_id)


def update(mix_id: int, **fields) -> Mix | None:
    with get_session() as s:
        mix = s.get(Mix, mix_id)
        if mix is None:
            return None
        for key, value in fields.items():
            setattr(mix, key, value)
        s.add(mix)
        s.commit()
        return mix


def all_mixes() -> list[Mix]:
    with get_session() as s:
        return list(s.exec(select(Mix).order_by(Mix.id.desc()).limit(30)).all())


def folder_of(mix: Mix) -> Path | None:
    folder = Path(mix.clip_path).parent
    return folder if folder.parent == config.MIXES_DIR else None


def retry_mix(mix_id: int) -> bool:
    mix = get_mix(mix_id)
    if not mix or mix.status not in ("failed", "cancelled"):
        return False
    update(mix_id, status="queued", progress=0, error=None, finished_at=None)
    return True


def delete_mix(mix_id: int) -> bool:
    mix = get_mix(mix_id)
    if not mix or mix.status in ("queued", "running"):
        return False
    folder = folder_of(mix)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    with get_session() as s:
        s.delete(s.get(Mix, mix_id))
        s.commit()
    return True


def requeue_interrupted() -> int:
    """After a restart a half-rendered mix is simply rendered again."""
    with get_session() as s:
        mixes = s.exec(select(Mix).where(Mix.status == "running")).all()
        for mix in mixes:
            mix.status, mix.progress = "queued", 0
            s.add(mix)
        s.commit()
        return len(mixes)


def to_media(mix: Mix) -> str:
    """Put the finished video into data/media for a publication (a hard link: no second copy of a big file)."""
    name = posting.media_name(f"mix_{mix.id}.mp4")
    dest = config.MEDIA_DIR / name
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.link(mix.video_path, dest)
    except OSError:  # another disk or a file system without hard links
        shutil.copy(mix.video_path, dest)
    return name


class MixWorker:
    """Renders queued mixes one by one in its own thread (ffmpeg may run for minutes)."""

    def __init__(self):
        self._event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._mix_id: int | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mixer", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        self._event.set()

    def cancel(self, mix_id: int) -> bool:
        mix = get_mix(mix_id)
        if not mix or mix.status not in ("queued", "running"):
            return False
        update(mix_id, status="cancelled", finished_at=utcnow())
        if self._mix_id == mix_id and self._proc:
            self._proc.terminate()
        return True

    def _run(self) -> None:
        while True:
            with get_session() as s:
                mix_id = s.exec(select(Mix.id).where(Mix.status == "queued").order_by(Mix.id)).first()
            if mix_id is None:
                self._event.wait()
                self._event.clear()
                continue
            try:
                self.render(mix_id)
            except Exception as e:  # nothing may stop this thread
                logger.exception("mix %s failed", mix_id)
                update(mix_id, status="failed", error=f"{type(e).__name__}: {e}"[:500], finished_at=utcnow())

    def render(self, mix_id: int) -> str:
        mix = update(mix_id, status="running", progress=0, error=None)
        folder = Path(mix.clip_path).parent
        out, log_path = folder / "mix.mp4", folder / "ffmpeg.log"
        try:
            clip = prepare_clip(Path(mix.clip_path), folder)
        except FileNotFoundError:
            return self._end(mix_id, "failed", error="no_ffmpeg")
        _, total = build_tracklist(mix.track_list, mix.crossfade)
        cmd = ffmpeg_command(clip, mix.track_list, mix.crossfade, total, out)
        self._mix_id = mix_id
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                              encoding="utf-8", errors="replace")
                shown = 0
                for line in self._proc.stdout:
                    percent = parse_progress(line, total)
                    if percent is not None and percent > shown:
                        shown = percent
                        update(mix_id, progress=percent)
                code = self._proc.wait()
        except FileNotFoundError:
            return self._end(mix_id, "failed", error="no_ffmpeg")
        finally:
            self._proc, self._mix_id = None, None
        if get_mix(mix_id).status == "cancelled":
            out.unlink(missing_ok=True)
            return "cancelled"
        if code != 0:
            out.unlink(missing_ok=True)
            error = log_path.read_text(encoding="utf-8", errors="replace").strip()[-500:] or f"ffmpeg exit code {code}"
            return self._end(mix_id, "failed", error=error)
        return self._end(mix_id, "done", video_path=str(out), progress=100)

    def _end(self, mix_id: int, status: str, **fields) -> str:
        mix = update(mix_id, status=status, finished_at=utcnow(), **fields)
        key = "notify.mix_done" if status == "done" else "notify.mix_failed"
        try:
            asyncio.run(notify(message(key, id=mix_id, title=mix.title or f"#{mix_id}", error=mix.error or "")))
        except Exception:
            logger.exception("mix notification failed")
        return status


worker = MixWorker()
