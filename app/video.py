"""Video inspection with ffprobe. Videos are uploaded as-is: we only warn.
The only exceptions: a cover frame for YouTube and shrinking a video that is too big for the Telegram bot."""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoInfo:
    duration: float  # seconds
    width: int
    height: int
    size_mb: float
    ext: str
    codec: str = ""


def probe(path: Path) -> VideoInfo:
    """Run ffprobe. Raises FileNotFoundError if ffprobe is not installed."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or "ffprobe failed")
    return parse_probe(json.loads(proc.stdout), path)


def parse_probe(data: dict, path: Path) -> VideoInfo:
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream is None:
        raise ValueError("no video stream")
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    # Phone videos often store rotation separately; swap sides for 90/270 degrees.
    rotation = int(stream.get("tags", {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    fmt = data.get("format", {})
    size = int(fmt.get("size") or path.stat().st_size)
    return VideoInfo(
        duration=float(fmt.get("duration") or stream.get("duration") or 0),
        width=width,
        height=height,
        size_mb=round(size / 1024 / 1024, 2),
        ext=path.suffix.lower().lstrip("."),
        codec=stream.get("codec_name", ""),
    )


def extract_frame(video: Path, second: float, dest: Path) -> Path:
    """Save one frame as a JPEG cover, at most 1280 px wide (YouTube's size, well under its 2 MB limit)."""
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, second):.2f}", "-i", str(video),
                           "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(dest)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not dest.exists():  # e.g. the second is past the end of the video
        raise ValueError(proc.stderr.strip()[-300:] or "no frame at this second")
    return dest


def shrink_plan(duration: float, target_mb: float, audio_kbps: int = 128) -> tuple[int, int] | None:
    """(video kbit/s, max height) that fit the video into target_mb, or None when it is too long for that."""
    video_kbps = int(target_mb * 8192 / max(duration, 1) * 0.95 - audio_kbps)  # 5% for the container
    if video_kbps < 150:
        return None
    return video_kbps, 1080 if video_kbps >= 3000 else 720 if video_kbps >= 1200 else 480


def shrink(video: Path, dest: Path, target_mb: float) -> Path:
    """Re-encode a smaller copy (H.264 + AAC) of about target_mb."""
    plan = shrink_plan(probe(video).duration, target_mb)
    if plan is None:
        raise ValueError(f"the video is too long to fit into {target_mb:.0f} MB")
    kbps, height = plan
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-c:v", "libx264", "-preset", "veryfast",
                           "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.2)}k", "-bufsize", f"{kbps * 2}k",
                           "-vf", f"scale=-2:'min({height},ih)'", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)],
                          capture_output=True, text=True, timeout=6 * 3600)
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise ValueError(proc.stderr.strip()[-300:] or "ffmpeg failed")
    return dest


def _ratio(value: str) -> float:
    w, h = value.split(":")
    return float(w) / float(h)


def check_video(info: VideoInfo, platform: str, rules: dict) -> list[dict]:
    """Return warnings like {"code": "duration", "platform": ..., "actual": ..., "limit": ...}."""
    v = rules.get("video", {})
    name = rules.get("name", platform)
    warnings = []
    max_dur = v.get("max_duration_sec")
    if max_dur and info.duration > max_dur:
        warnings.append({"code": "duration", "platform": name,
                         "actual": round(info.duration), "limit": max_dur})
    max_size = v.get("max_size_mb")
    if max_size and info.size_mb > max_size:
        code = "size_shrink" if v.get("shrink") else "size"  # the poster makes a smaller copy itself
        warnings.append({"code": code, "platform": name, "actual": info.size_mb, "limit": max_size})
    formats = v.get("formats")
    if formats and info.ext not in formats:
        warnings.append({"code": "format", "platform": name, "actual": info.ext,
                         "limit": ", ".join(formats)})
    ratios = v.get("aspect_ratios")
    if ratios and info.width and info.height:
        actual = info.width / info.height
        if not any(abs(actual - _ratio(r)) / _ratio(r) < 0.03 for r in ratios):
            warnings.append({"code": "aspect", "platform": name,
                             "actual": f"{info.width}x{info.height}", "limit": ", ".join(ratios)})
    return warnings
