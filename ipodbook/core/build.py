"""The MP3 -> M4B build pipeline.

Source files are decoded one at a time to headerless PCM and piped into a
single encoder process. That indirection is the whole point: ffmpeg's concat
demuxer derives its timeline from container metadata, and on MP3s without a
Xing header those durations are estimates. Feeding it 227 such files produced
1,630 non-monotonic DTS warnings and silently dropped five minutes of audio.
Raw PCM carries no timestamps, so there is nothing to drift.
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import discover, ffmpeg, limits, measure, tags, verify
from .measure import Cancelled, TrackInfo

#: ``progress(phase, fraction, detail)`` -- fraction is 0..1 within the phase.
ProgressFn = Callable[[str, float, str], None]

PHASE_MEASURE = "measure"
PHASE_ENCODE = "encode"
PHASE_TAG = "tag"
PHASE_VERIFY = "verify"


@dataclass
class Settings:
    """Everything tunable about an output file."""

    sample_rate: int = 22050
    bitrate_kbps: int = 40
    channels: int = 1
    encoder: str = "aac"
    device_key: str = "ipod"
    chapters: bool = True
    chapter_style: str = "folder"  # filename | folder | number

    @property
    def device(self) -> limits.Device:
        return limits.device_by_key(self.device_key)


class BuildError(RuntimeError):
    """A build failed for a reason worth showing the user verbatim."""


@dataclass
class BuildResult:
    output: Path
    duration_s: float
    samples: int
    size_bytes: int
    chapters: int


def _noop(phase: str, fraction: float, detail: str) -> None:  # pragma: no cover
    pass


def _decode_cmd(path: Path, rate: int, channels: int) -> list[str]:
    return [
        ffmpeg.ffmpeg_path(), "-v", "error", "-nostdin",
        "-i", str(path),
        "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-",
    ]


def _encode_cmd(
    settings: Settings, pipe_rate: int, ffmeta: Path | None, target: Path
) -> list[str]:
    cmd = [
        ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
        "-f", "s16le", "-ar", str(pipe_rate), "-ac", str(settings.channels),
        "-i", "pipe:0",
    ]
    if ffmeta is not None:
        cmd += ["-i", str(ffmeta)]
    cmd += ["-map", "0:a"]
    if ffmeta is not None:
        cmd += ["-map_chapters", "1"]
    cmd += ["-c:a", settings.encoder]
    # Apple's encoder rejects the profile name as a string and defaults to
    # AAC-LC anyway; ffmpeg's native encoder takes it and we set it explicitly
    # so no build can ever emit HE-AAC, which old iPods cannot decode.
    if settings.encoder == "aac":
        cmd += ["-profile:a", "aac_low"]
    cmd += [
        "-b:a", f"{settings.bitrate_kbps}k",
        "-ar", str(settings.sample_rate),
        "-ac", str(settings.channels),
        "-movflags", "+faststart",
        "-nostats", "-progress", "pipe:1",
        "-f", "mp4", str(target),
    ]
    return cmd


def _watch_progress(
    stream, total_s: float, progress: ProgressFn, cancel: threading.Event
) -> None:
    """Translate ffmpeg's -progress stream into fractional updates."""
    for raw in iter(stream.readline, b""):
        if cancel.is_set():
            return
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("out_time_us=") and not line.startswith("out_time_ms="):
            continue
        _, _, value = line.partition("=")
        try:
            micros = int(value)
        except ValueError:
            continue
        # ffmpeg's out_time_ms is actually microseconds; both keys agree here.
        done = micros / 1_000_000
        fraction = min(1.0, done / total_s) if total_s > 0 else 0.0
        progress(
            PHASE_ENCODE,
            fraction,
            f"{limits.format_duration(done)} / {limits.format_duration(total_s)}",
        )


def _run_pipeline(
    tracks: Sequence[TrackInfo],
    settings: Settings,
    pipe_rate: int,
    ffmeta: Path | None,
    target: Path,
    total_s: float,
    progress: ProgressFn,
    cancel: threading.Event,
) -> None:
    """Stream every decoded track through one encoder process."""
    encoder = subprocess.Popen(
        _encode_cmd(settings, pipe_rate, ffmeta, target),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert encoder.stdin is not None and encoder.stdout is not None

    watcher = threading.Thread(
        target=_watch_progress,
        args=(encoder.stdout, total_s, progress, cancel),
        daemon=True,
    )
    watcher.start()

    current: subprocess.Popen | None = None
    try:
        for track in tracks:
            if cancel.is_set():
                raise Cancelled("build cancelled")
            # The child writes straight into the encoder's stdin fd; no audio
            # data passes through this process.
            current = subprocess.Popen(
                _decode_cmd(track.path, pipe_rate, settings.channels),
                stdout=encoder.stdin,
                stderr=subprocess.PIPE,
            )
            _, err = current.communicate()
            if current.returncode != 0:
                message = err.decode("utf-8", "replace").strip()[:300]
                raise BuildError(f"Could not decode {track.path.name}: {message}")
            current = None
    except BaseException:
        if current is not None and current.poll() is None:
            current.kill()
        encoder.kill()
        raise
    finally:
        try:
            encoder.stdin.close()
        except OSError:
            pass

    encoder_err = encoder.stderr.read().decode("utf-8", "replace") if encoder.stderr else ""
    encoder.wait()
    watcher.join(timeout=2)
    if encoder.stderr:
        encoder.stderr.close()
    encoder.stdout.close()

    if cancel.is_set():
        raise Cancelled("build cancelled")
    if encoder.returncode != 0:
        raise BuildError(f"Encoding failed:\n{encoder_err.strip()[:600]}")


def build(
    paths: Sequence[Path] | Sequence[TrackInfo],
    output: Path,
    settings: Settings,
    metadata: tags.Metadata | None = None,
    *,
    progress: ProgressFn = _noop,
    cancel: threading.Event | None = None,
    overwrite: bool = False,
) -> BuildResult:
    """Build an audiobook. Returns once the finished file is in place.

    The output is assembled under a temporary name in the destination directory
    and moved into place only after verification, so an interrupted or failed
    build never leaves a partial ``.m4b`` behind.
    """
    cancel = cancel or threading.Event()
    metadata = metadata or tags.Metadata()
    output = Path(output).expanduser()

    if not paths:
        raise BuildError("No source files selected.")
    if output.exists() and not overwrite:
        raise BuildError(f"{output.name} already exists.")
    if not output.parent.is_dir():
        raise BuildError(f"Destination folder does not exist:\n{output.parent}")
    if not os.access(output.parent, os.W_OK):
        raise BuildError(f"Destination folder is not writable:\n{output.parent}")
    if settings.encoder not in ffmpeg.available_encoders():
        raise BuildError(f"Encoder {settings.encoder!r} is unavailable in this ffmpeg build.")

    tracks: list[TrackInfo] = [
        t if isinstance(t, TrackInfo) else TrackInfo(path=Path(t)) for t in paths
    ]
    for track in tracks:
        if not track.path.is_file():
            raise BuildError(f"Missing source file:\n{track.path}")

    # --- measure -----------------------------------------------------------
    pending = [t for t in tracks if not t.exact]
    if pending:
        progress(PHASE_MEASURE, 0.0, f"0 / {len(pending)} files")
        for track in pending:
            if not track.sample_rate:
                probed = measure.probe_quick(track.path)
                track.sample_rate = probed.sample_rate
                track.channels = probed.channels
                track.codec = probed.codec
        measure.measure_all(
            pending,
            progress=lambda done, total: progress(
                PHASE_MEASURE, done / total, f"{done} / {total} files"
            ),
            cancel=cancel,
        )
    broken = [t for t in tracks if not t.ok]
    if broken:
        names = ", ".join(t.path.name for t in broken[:3])
        raise BuildError(f"Could not read {len(broken)} file(s): {names}")

    total_s = measure.total_seconds(tracks)
    if total_s <= 0:
        raise BuildError("Source files contain no audio.")

    # --- budget check ------------------------------------------------------
    device = settings.device
    samples = limits.sample_count(total_s, settings.sample_rate)
    if not limits.fits(total_s, settings.sample_rate, device.max_samples):
        suggestion = limits.best_rate(total_s, device.max_samples)
        hint = (
            f" Use {suggestion / 1000:g} kHz or lower."
            if suggestion
            else " Shorten the book or split it."
        )
        raise BuildError(
            f"{limits.format_duration(total_s)} at {settings.sample_rate / 1000:g} kHz "
            f"needs {limits.format_samples(samples)} samples, over this device's "
            f"{limits.format_samples(device.max_samples)} limit.{hint}"
        )

    pipe_rate = measure.pipeline_rate(tracks)
    temp = output.parent / f".{output.stem}.{uuid.uuid4().hex[:8]}.tmp.m4b"
    ffmeta_path: Path | None = None

    try:
        # --- chapters ------------------------------------------------------
        if settings.chapters:
            root = discover.common_root([t.path for t in tracks])
            titles = [
                discover.chapter_title(t.path, settings.chapter_style, i, root=root)
                for i, t in enumerate(tracks)
            ]
            chapter_list = tags.chapters_from_durations(
                [t.seconds for t in tracks], titles
            )
            ffmeta_path = temp.with_suffix(".ffmeta")
            tags.write_ffmetadata(ffmeta_path, chapter_list)
        else:
            chapter_list = []

        # --- encode --------------------------------------------------------
        progress(PHASE_ENCODE, 0.0, "starting")
        _run_pipeline(
            tracks, settings, pipe_rate, ffmeta_path, temp, total_s, progress, cancel
        )

        # --- tag -----------------------------------------------------------
        progress(PHASE_TAG, 0.5, "writing metadata")
        tags.apply_tags(temp, metadata)

        # --- verify --------------------------------------------------------
        progress(PHASE_VERIFY, 0.5, "checking output")
        report = verify.check(
            temp,
            expected_seconds=total_s,
            expected_chapters=len(chapter_list),
            expected_rate=settings.sample_rate,
            expected_channels=settings.channels,
            max_samples=device.max_samples,
        )
        if not report.ok:
            raise BuildError("Output failed verification:\n" + "\n".join(report.problems))

        os.replace(temp, output)
        progress(PHASE_VERIFY, 1.0, "done")
        return BuildResult(
            output=output,
            duration_s=report.duration_s,
            samples=report.samples,
            size_bytes=report.size_bytes,
            chapters=report.chapters,
        )
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        if ffmeta_path is not None:
            ffmeta_path.unlink(missing_ok=True)
