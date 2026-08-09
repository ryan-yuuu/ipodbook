"""Post-build checks.

A build is only considered successful if the finished file actually plays back
the way it was asked to. These checks run before the temporary file is moved
into place, so a file that fails them is never delivered.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg, limits

#: Tolerance between requested and produced duration. Encoder priming and
#: end-of-stream padding move the total by a handful of samples.
DURATION_TOLERANCE_S = 2.0


@dataclass
class Report:
    duration_s: float = 0.0
    samples: int = 0
    size_bytes: int = 0
    chapters: int = 0
    codec: str = ""
    profile: str = ""
    sample_rate: int = 0
    channels: int = 0
    mdhd_version: int = 0
    faststart: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _top_level_atoms(path: Path) -> list[tuple[str, int, int]]:
    """(type, offset, size) for each top-level MP4 atom."""
    atoms: list[tuple[str, int, int]] = []
    total = path.stat().st_size
    with path.open("rb") as handle:
        offset = 0
        while offset < total:
            handle.seek(offset)
            header = handle.read(16)
            if len(header) < 8:
                break
            size = struct.unpack(">I", header[0:4])[0]
            kind = header[4:8].decode("latin1", "replace")
            if size == 1:
                size = struct.unpack(">Q", header[8:16])[0]
            if size < 8:
                break
            atoms.append((kind, offset, size))
            offset += size
    return atoms


def _mdhd_version(path: Path, moov: tuple[int, int]) -> int:
    """Version byte of the audio track's media header.

    ffmpeg emits a 64-bit (version 1) header once the duration exceeds
    INT32_MAX, which is precisely the condition old iPods choke on -- so this
    doubles as a second signal that a file is over budget.
    """
    start, size = moov
    with path.open("rb") as handle:
        def walk(begin: int, end: int) -> int | None:
            offset = begin
            while offset < end:
                handle.seek(offset)
                header = handle.read(16)
                if len(header) < 8:
                    return None
                atom_size = struct.unpack(">I", header[0:4])[0]
                kind = header[4:8].decode("latin1", "replace")
                head = 8
                if atom_size == 1:
                    atom_size = struct.unpack(">Q", header[8:16])[0]
                    head = 16
                if atom_size < 8:
                    return None
                if kind == "mdhd":
                    handle.seek(offset + 8)
                    return handle.read(1)[0]
                if kind in {"moov", "trak", "mdia", "minf", "stbl"}:
                    found = walk(offset + head, offset + atom_size)
                    if found is not None:
                        return found
                offset += atom_size
            return None

        return walk(start + 8, start + size) or 0


def check(
    path: Path,
    *,
    expected_seconds: float,
    expected_chapters: int,
    expected_rate: int,
    expected_channels: int,
    max_samples: int | None,
) -> Report:
    """Inspect a finished file and collect anything wrong with it."""
    report = Report()
    path = Path(path)

    if not path.is_file() or path.stat().st_size == 0:
        report.problems.append("Output file is missing or empty.")
        return report

    report.size_bytes = path.stat().st_size

    try:
        data = ffmpeg.probe(path, chapters=True)
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"Output is not readable: {exc}")
        return report

    audio = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if audio is None:
        report.problems.append("Output contains no audio stream.")
        return report

    report.codec = audio.get("codec_name", "")
    report.profile = audio.get("profile", "") or ""
    report.sample_rate = int(audio.get("sample_rate", 0) or 0)
    report.channels = int(audio.get("channels", 0) or 0)
    report.chapters = len(data.get("chapters", []))
    try:
        report.duration_s = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        report.problems.append("Output has no readable duration.")
        return report

    report.samples = limits.sample_count(report.duration_s, report.sample_rate)

    if report.codec != "aac":
        report.problems.append(f"Expected AAC, produced {report.codec!r}.")
    if report.profile and report.profile != "LC":
        # HE-AAC decodes as silence or noise on pre-2007 iPods.
        report.problems.append(
            f"Expected AAC-LC, produced {report.profile!r}, which old iPods cannot decode."
        )
    if report.sample_rate != expected_rate:
        report.problems.append(
            f"Expected {expected_rate} Hz, produced {report.sample_rate} Hz."
        )
    if report.channels != expected_channels:
        report.problems.append(
            f"Expected {expected_channels} channel(s), produced {report.channels}."
        )
    if abs(report.duration_s - expected_seconds) > DURATION_TOLERANCE_S:
        report.problems.append(
            f"Duration drifted: expected {limits.format_duration(expected_seconds)}, "
            f"produced {limits.format_duration(report.duration_s)}."
        )
    if expected_chapters and report.chapters != expected_chapters:
        report.problems.append(
            f"Expected {expected_chapters} chapters, produced {report.chapters}."
        )
    if max_samples is not None and report.samples >= max_samples:
        report.problems.append(
            f"Output holds {limits.format_samples(report.samples)} samples, at or over "
            f"the {limits.format_samples(max_samples)} device limit."
        )

    atoms = _top_level_atoms(path)
    kinds = [kind for kind, _, _ in atoms]
    if "moov" in kinds and "mdat" in kinds:
        report.faststart = kinds.index("moov") < kinds.index("mdat")
        if not report.faststart:
            report.problems.append("Index was not moved to the front of the file.")
        moov = next((off, size) for kind, off, size in atoms if kind == "moov")
        report.mdhd_version = _mdhd_version(path, moov)
        if max_samples is not None and report.mdhd_version != 0:
            report.problems.append(
                "Track header is 64-bit, which pre-2007 iPod firmware may not parse."
            )
    else:
        report.problems.append("Output is not a well-formed MP4 file.")

    return report
