"""Video inspection with ffprobe. Videos are uploaded as-is: we only warn, never convert."""
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
        warnings.append({"code": "size", "platform": name, "actual": info.size_mb, "limit": max_size})
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
