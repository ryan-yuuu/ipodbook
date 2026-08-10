"""Locating and invoking ffmpeg / ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

#: Places Homebrew and friends install to that may not be on a GUI app's PATH.
_EXTRA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


class FFmpegMissing(RuntimeError):
    """Raised when ffmpeg or ffprobe cannot be found."""


def _locate(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in _EXTRA_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


@lru_cache(maxsize=None)
def ffmpeg_path() -> str:
    path = _locate("ffmpeg")
    if not path:
        raise FFmpegMissing(
            "ffmpeg was not found. Install it with:  brew install ffmpeg"
        )
    return path


@lru_cache(maxsize=None)
def ffprobe_path() -> str:
    path = _locate("ffprobe")
    if not path:
        raise FFmpegMissing(
            "ffprobe was not found. Install it with:  brew install ffmpeg"
        )
    return path


def have_ffmpeg() -> bool:
    try:
        ffmpeg_path()
        ffprobe_path()
    except FFmpegMissing:
        return False
    return True


@lru_cache(maxsize=None)
def available_encoders() -> tuple[str, ...]:
    """AAC encoders this ffmpeg build offers, best first.

    ``aac_at`` is Apple's AudioToolbox encoder (macOS only): slightly more
    efficient than ffmpeg's native encoder at the same bitrate, and it produces
    plain AAC-LC rather than HE-AAC, which old iPods cannot decode.
    """
    try:
        out = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=False,
        ).stdout
    except FFmpegMissing:
        return ()
    found = []
    for name in ("aac_at", "aac"):
        if f" {name} " in out:
            found.append(name)
    return tuple(found)


ENCODER_LABELS = {
    "aac_at": "Apple AudioToolbox",
    "aac": "ffmpeg native",
}


def encoder_label(name: str) -> str:
    return ENCODER_LABELS.get(name, name)


def probe(path: str | Path, *, streams: bool = True, chapters: bool = False) -> dict:
    """Run ffprobe and return its parsed JSON."""
    cmd = [ffprobe_path(), "-v", "quiet", "-print_format", "json", "-show_format"]
    if streams:
        cmd.append("-show_streams")
    if chapters:
        cmd.append("-show_chapters")
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {Path(path).name}")
    return json.loads(result.stdout)


def audio_stream(data: dict, name: str = "file") -> dict:
    """Codec, sample rate and channel count from an already-parsed probe.

    Audiobook m4b files routinely carry a chapter text track, a cover image and
    a binary data track alongside the audio, so the first stream is not
    necessarily the one we want.
    """
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "audio":
            return {
                "codec": stream.get("codec_name"),
                "sample_rate": int(stream.get("sample_rate", 0) or 0),
                "channels": int(stream.get("channels", 0) or 0),
                "profile": stream.get("profile"),
            }
    raise RuntimeError(f"no audio stream in {name}")


def container_duration(data: dict) -> float:
    """Container-reported duration from an already-parsed probe."""
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def audio_stream_info(path: str | Path) -> dict:
    """Codec, sample rate and channel count of the file's audio stream."""
    return audio_stream(probe(path), Path(path).name)


def estimated_duration(path: str | Path) -> float:
    """Container-reported duration.

    Unreliable for MP3s lacking a Xing/Info header -- ffprobe falls back to
    estimating from bitrate, which ran ~0.5% low on real CD rips. Good enough
    for instant feedback, never for chapter boundaries. Use ``measure`` for those.
    """
    return container_duration(probe(path, streams=False))


def extract_cover(path: str | Path) -> bytes:
    """Embedded cover art as image bytes, or empty if the file has none.

    Goes through ffmpeg rather than a tag library so MP3, FLAC and MP4 sources
    all work through one path: in every container ffmpeg exposes attached
    artwork as a video stream that can be copied straight out.
    """
    result = subprocess.run(
        [ffmpeg_path(), "-v", "error", "-nostdin", "-i", str(path),
         "-map", "0:v:0", "-frames:v", "1", "-c", "copy", "-f", "image2pipe", "-"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return b""  # no attached picture; not an error worth surfacing
    return result.stdout
