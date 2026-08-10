"""Measuring how long each source file actually is.

Container metadata cannot be trusted here. MP3s ripped from CD frequently lack
a Xing/Info header, so ffprobe estimates duration from the nominal bitrate --
on real rips that ran about 0.5% short per file, which compounded to a five
minute error across 227 tracks and silently misplaced every chapter marker.

The only reliable length is the one you get by decoding. These helpers decode
each file to raw PCM and count the samples that come out.
"""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import ffmpeg

#: Reference rate used for measurement. The build pipeline decodes at the same
#: rate, so measured counts and encoded output stay consistent.
DEFAULT_REFERENCE_RATE = 44100

_CHUNK = 1 << 20  # 1 MiB


class Cancelled(RuntimeError):
    """Raised when the caller aborts a measurement or build."""


@dataclass(frozen=True)
class SourceChapter:
    """A chapter already present inside a source file.

    Times are on the *container's* timeline, which is not quite the timeline of
    the audio we decode out of it -- see ``build`` for why the two differ.
    """

    start: float
    end: float
    title: str

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TrackInfo:
    """One source file and everything we know about it."""

    path: Path
    seconds: float = 0.0
    exact: bool = False        # True once measured by decoding
    sample_rate: int = 0
    channels: int = 0
    codec: str = ""
    error: str = ""
    chapters: list[SourceChapter] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def _parse_chapters(data: dict) -> list[SourceChapter]:
    """Chapter entries from a parsed ffprobe result, in file order."""
    found: list[SourceChapter] = []
    for raw in data.get("chapters", []):
        try:
            start = float(raw["start_time"])
            end = float(raw["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        title = (raw.get("tags") or {}).get("title") or ""
        found.append(SourceChapter(start=start, end=end, title=title))
    return found


def probe_quick(path: Path) -> TrackInfo:
    """Instant, approximate metadata for immediate UI feedback.

    One ffprobe call collects the stream layout, the container duration and any
    embedded chapters together, so knowing a file's chapters costs nothing
    beyond what reading its duration already cost.
    """
    info = TrackInfo(path=path)
    try:
        data = ffmpeg.probe(path, chapters=True)
        stream = ffmpeg.audio_stream(data, path.name)
        info.sample_rate = stream["sample_rate"]
        info.channels = stream["channels"]
        info.codec = stream["codec"] or ""
        info.seconds = ffmpeg.container_duration(data)
        info.chapters = _parse_chapters(data)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        info.error = str(exc)
    return info


def measure_exact(
    path: Path,
    *,
    reference_rate: int = DEFAULT_REFERENCE_RATE,
    cancel: threading.Event | None = None,
) -> float:
    """Decode a file and return its true duration in seconds.

    Decoding to mono keeps the byte count proportional to time regardless of
    the source's channel layout.
    """
    cmd = [
        ffmpeg.ffmpeg_path(), "-v", "error", "-nostdin",
        "-i", str(path),
        "-f", "s16le", "-ar", str(reference_rate), "-ac", "1", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total = 0
    try:
        assert proc.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled("measurement cancelled")
            chunk = proc.stdout.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
    finally:
        if proc.stdout:
            proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"could not decode {path.name}: {stderr.strip()[:200]}")
    return total / 2 / reference_rate  # 2 bytes per s16 sample


def measure_all(
    tracks: Sequence[TrackInfo],
    *,
    reference_rate: int = DEFAULT_REFERENCE_RATE,
    workers: int = 8,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> list[TrackInfo]:
    """Measure every track exactly, in parallel, updating each in place.

    ``progress`` receives ``(completed, total)`` after each file.
    """
    total = len(tracks)
    done = 0
    lock = threading.Lock()

    def work(track: TrackInfo) -> None:
        nonlocal done
        try:
            track.seconds = measure_exact(
                track.path, reference_rate=reference_rate, cancel=cancel
            )
            track.exact = True
            track.error = ""
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            track.error = str(exc)
        finally:
            with lock:
                done += 1
                if progress is not None:
                    progress(done, total)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, tracks))

    if cancel is not None and cancel.is_set():
        raise Cancelled("measurement cancelled")
    return list(tracks)


def total_seconds(tracks: Sequence[TrackInfo]) -> float:
    return sum(t.seconds for t in tracks if t.ok)


def all_exact(tracks: Sequence[TrackInfo]) -> bool:
    return bool(tracks) and all(t.exact for t in tracks if t.ok)


def pipeline_rate(tracks: Sequence[TrackInfo]) -> int:
    """Intermediate PCM rate for the build.

    When every source shares a sample rate we pipe at that rate, so the audio
    is resampled exactly once on its way to the target. Mixed sources fall back
    to the highest rate present, which avoids upsampling anything twice.
    """
    rates = {t.sample_rate for t in tracks if t.ok and t.sample_rate}
    if not rates:
        return DEFAULT_REFERENCE_RATE
    return max(rates)


def source_summary(tracks: Sequence[TrackInfo]) -> str:
    """Human description of the source set, for a preflight warning."""
    rates = sorted({t.sample_rate for t in tracks if t.ok and t.sample_rate})
    channels = sorted({t.channels for t in tracks if t.ok and t.channels})
    bits = []
    if rates:
        bits.append("/".join(f"{r/1000:g} kHz" for r in rates))
    if channels:
        bits.append("/".join("mono" if c == 1 else "stereo" if c == 2 else f"{c}ch" for c in channels))
    return ", ".join(bits)
