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
import stat
import subprocess
import threading
import uuid
from dataclasses import dataclass, replace
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
    chapter_style: str = "folder"  # filename | folder | number | embedded
    #: How many files to split the book across. ``1`` is a single file, which
    #: fails if the book is over budget; ``None`` picks the fewest volumes that
    #: fit with headroom to spare.
    volumes: int | None = 1

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


def _unhide(path: Path) -> None:
    """Clear the macOS hidden flag from a finished file.

    The build assembles output under a dot-prefixed temporary name so a partial
    file neither clutters the destination nor gets uploaded by a sync client.
    In an iCloud Drive folder, though, macOS's FileProvider daemon notices such
    files and sets ``UF_HIDDEN`` on them within about a minute -- and rename
    preserves flags. Without this, a long build into iCloud Drive would produce
    a perfectly good file that never appears in Finder.

    No-op on platforms without BSD file flags.
    """
    hidden = getattr(stat, "UF_HIDDEN", 0)
    if not hidden or not hasattr(os, "chflags"):
        return
    try:
        flags = os.stat(path).st_flags
    except (OSError, AttributeError):
        return
    if flags & hidden:
        try:
            os.chflags(path, flags & ~hidden)
        except OSError:
            pass  # cosmetic only; never fail a good build over it


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


def volume_path(output: Path, index: int, total: int) -> Path:
    """Where volume ``index`` of ``total`` is written.

    A single volume keeps the name the user chose; several are numbered so they
    sort in reading order in any file browser.
    """
    if total <= 1:
        return output
    return output.with_name(f"{output.stem} - Vol {index} of {total}{output.suffix}")


def _volume_metadata(
    metadata: tags.Metadata, output: Path, index: int, total: int
) -> tags.Metadata:
    """Per-volume tags: shared album, distinct title, ordered track number.

    Players group by album and order by track number, so a split book stays a
    single entry in the library with its volumes in sequence rather than
    scattering as unrelated titles.
    """
    if total <= 1:
        return metadata
    base = metadata.title.strip() or metadata.album.strip() or output.stem
    volume = replace(metadata)
    volume.album = metadata.album.strip() or base
    volume.title = f"{base} - Vol {index} of {total}"
    volume.track = (index, total)
    return volume


def _prepare(
    paths: Sequence[Path] | Sequence[TrackInfo],
    settings: Settings,
    progress: ProgressFn,
    cancel: threading.Event,
) -> list[TrackInfo]:
    """Validate the source list and measure every file exactly."""
    if not paths:
        raise BuildError("No source files selected.")
    if settings.encoder not in ffmpeg.available_encoders():
        raise BuildError(f"Encoder {settings.encoder!r} is unavailable in this ffmpeg build.")

    tracks: list[TrackInfo] = [
        t if isinstance(t, TrackInfo) else TrackInfo(path=Path(t)) for t in paths
    ]
    for track in tracks:
        if not track.path.is_file():
            raise BuildError(f"Missing source file:\n{track.path}")

    pending = [t for t in tracks if not t.exact]
    if pending:
        progress(PHASE_MEASURE, 0.0, f"0 / {len(pending)} files")
        for track in pending:
            if not track.sample_rate:
                probed = measure.probe_quick(track.path)
                track.sample_rate = probed.sample_rate
                track.channels = probed.channels
                track.codec = probed.codec
                track.chapters = probed.chapters
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

    if measure.total_seconds(tracks) <= 0:
        raise BuildError("Source files contain no audio.")
    return tracks


def _check_destination(
    output: Path, tracks: Sequence[TrackInfo], *, overwrite: bool
) -> None:
    """Refuse a destination that cannot be written, or that is also a source.

    The last case is easy to hit when merging: the finished book lands in the
    folder it was built from, and the next scan of that folder picks it up as
    another chapter.
    """
    if not output.parent.is_dir():
        raise BuildError(f"Destination folder does not exist:\n{output.parent}")
    if not os.access(output.parent, os.W_OK):
        raise BuildError(f"Destination folder is not writable:\n{output.parent}")
    if output.exists() and not overwrite:
        raise BuildError(f"{output.name} already exists.")

    resolved = output.resolve()
    if any(track.path.resolve() == resolved for track in tracks):
        raise BuildError(
            f"{output.name} is also one of the source files.\n"
            "Choose a destination outside the source list."
        )


def _build_one(
    tracks: Sequence[TrackInfo],
    output: Path,
    settings: Settings,
    metadata: tags.Metadata,
    pipe_rate: int,
    progress: ProgressFn,
    cancel: threading.Event,
) -> BuildResult:
    """Assemble, tag and verify one output file, then move it into place."""
    total_s = measure.total_seconds(tracks)
    temp = output.parent / f".{output.stem}.{uuid.uuid4().hex[:8]}.tmp.m4b"
    ffmeta_path: Path | None = None

    try:
        # --- chapters ------------------------------------------------------
        if settings.chapters:
            root = discover.common_root([t.path for t in tracks])
            titles = [
                discover.chapter_title(
                    t.path, settings.chapter_style, i, root=root, chapters=t.chapters
                )
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
            max_samples=settings.device.max_samples,
        )
        if not report.ok:
            raise BuildError("Output failed verification:\n" + "\n".join(report.problems))

        os.replace(temp, output)
        _unhide(output)
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


def _over_budget_error(total_s: float, settings: Settings) -> BuildError:
    device = settings.device
    samples = limits.sample_count(total_s, settings.sample_rate)
    suggestion = limits.best_rate(total_s, device.max_samples)
    hint = (
        f" Use {suggestion / 1000:g} kHz or lower, or split it into volumes."
        if suggestion
        else " Split it into volumes."
    )
    return BuildError(
        f"{limits.format_duration(total_s)} at {settings.sample_rate / 1000:g} kHz "
        f"needs {limits.format_samples(samples)} samples, over this device's "
        f"{limits.format_samples(device.max_samples)} limit.{hint}"
    )


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
    """Build a single audiobook file. Returns once it is in place.

    The output is assembled under a temporary name in the destination directory
    and moved into place only after verification, so an interrupted or failed
    build never leaves a partial ``.m4b`` behind.
    """
    cancel = cancel or threading.Event()
    metadata = metadata or tags.Metadata()
    output = Path(output).expanduser()

    tracks = _prepare(paths, settings, progress, cancel)
    _check_destination(output, tracks, overwrite=overwrite)

    total_s = measure.total_seconds(tracks)
    if not limits.fits(total_s, settings.sample_rate, settings.device.max_samples):
        raise _over_budget_error(total_s, settings)

    return _build_one(
        tracks, output, settings, metadata,
        measure.pipeline_rate(tracks), progress, cancel,
    )


def plan(
    tracks: Sequence[TrackInfo], settings: Settings
) -> list[list[int]]:
    """Group measured tracks into volumes according to ``settings``."""
    return limits.plan_volumes(
        [t.seconds for t in tracks],
        settings.sample_rate,
        settings.device.max_samples,
        volumes=settings.volumes,
    )


def build_volumes(
    paths: Sequence[Path] | Sequence[TrackInfo],
    output: Path,
    settings: Settings,
    metadata: tags.Metadata | None = None,
    *,
    progress: ProgressFn = _noop,
    cancel: threading.Event | None = None,
    overwrite: bool = False,
) -> list[BuildResult]:
    """Build the book, splitting it across volumes when it cannot fit in one.

    Every destination is checked before any encoding starts, so a run cannot get
    most of the way through and then fail because volume 3 already existed.
    Volumes are written one at a time and each is verified before the next
    begins; a failure part way leaves the volumes already finished in place,
    since they are complete files in their own right.
    """
    cancel = cancel or threading.Event()
    metadata = metadata or tags.Metadata()
    output = Path(output).expanduser()

    tracks = _prepare(paths, settings, progress, cancel)

    # Asking for one file and not fitting in one file is the plain over-budget
    # case, and deserves the message that names a rate that would work.
    total_s = measure.total_seconds(tracks)
    if settings.volumes == 1 and not limits.fits(
        total_s, settings.sample_rate, settings.device.max_samples
    ):
        raise _over_budget_error(total_s, settings)

    try:
        groups = plan(tracks, settings)
    except limits.CannotSplit as exc:
        raise BuildError(str(exc)) from exc

    total = len(groups)
    destinations = [volume_path(output, i, total) for i in range(1, total + 1)]
    seen: set[Path] = set()
    for destination in destinations:
        if destination in seen:
            raise BuildError(f"Two volumes would be written to {destination.name}.")
        seen.add(destination)
        _check_destination(destination, tracks, overwrite=overwrite)

    pipe_rate = measure.pipeline_rate(tracks)
    results: list[BuildResult] = []
    for index, (group, destination) in enumerate(zip(groups, destinations), 1):
        results.append(
            _build_one(
                [tracks[i] for i in group],
                destination,
                settings,
                _volume_metadata(metadata, output, index, total),
                pipe_rate,
                _prefixed(progress, index, total),
                cancel,
            )
        )
    return results


def _prefixed(progress: ProgressFn, index: int, total: int) -> ProgressFn:
    """Tag progress detail with which volume it refers to."""
    if total <= 1:
        return progress

    def relay(phase: str, fraction: float, detail: str) -> None:
        progress(phase, fraction, f"Vol {index}/{total} — {detail}")

    return relay
