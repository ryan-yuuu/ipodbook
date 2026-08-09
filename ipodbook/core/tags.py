"""Chapter markers and MP4 metadata.

Responsibilities are split deliberately:

* **Chapters** go through ffmpeg, via an ffmetadata file -- it is the only
  component here that can write an MP4 chapter track.
* **Everything else** (tags, the audiobook flag, cover art) is written
  afterwards with mutagen.

The split exists because ffmpeg's MP4 muxer silently drops freeform iTunes
atoms such as ``publisher``, and the ``use_metadata_tags`` movflag that would
preserve them discards embedded cover art instead. Writing tags separately
avoids having to choose.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Sequence

#: MP4 ``stik`` value that marks a file as an audiobook. This is what makes a
#: player remember your position and file it under Audiobooks rather than Music.
STIK_AUDIOBOOK = 2

#: Optional free-text tags -> iTunes atom names.
_ATOMS = {
    "title": "\xa9nam",
    "author": "\xa9ART",
    "album_artist": "aART",
    "narrator": "\xa9wrt",
    "album": "\xa9alb",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "comment": "\xa9cmt",
    "description": "desc",
    "synopsis": "ldes",
}

#: Tags with no standard atom, stored as iTunes freeform ``----`` atoms.
_FREEFORM = {
    "publisher": "----:com.apple.iTunes:publisher",
}


@dataclass
class Metadata:
    """Optional descriptive tags. Blank fields are simply not written."""

    title: str = ""
    author: str = ""
    narrator: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    comment: str = ""
    description: str = ""   # short blurb -> desc
    synopsis: str = ""      # long blurb  -> ldes
    publisher: str = ""
    cover_path: Path | None = None

    def is_empty(self) -> bool:
        return not any(
            getattr(self, f.name) for f in fields(self) if f.name != "cover_path"
        ) and self.cover_path is None


@dataclass
class Chapter:
    """A chapter boundary in milliseconds, end-exclusive."""

    start_ms: int
    end_ms: int
    title: str


def chapters_from_durations(
    seconds: Sequence[float], titles: Sequence[str]
) -> list[Chapter]:
    """Lay tracks end to end, deriving boundaries from measured durations.

    Boundaries are accumulated in floating seconds and rounded only at the
    edges, so rounding never drifts across hundreds of tracks.
    """
    if len(seconds) != len(titles):
        raise ValueError("durations and titles must be the same length")
    chapters: list[Chapter] = []
    elapsed = 0.0
    for duration, title in zip(seconds, titles):
        start = elapsed
        elapsed += duration
        chapters.append(
            Chapter(
                start_ms=int(round(start * 1000)),
                end_ms=int(round(elapsed * 1000)),
                title=title,
            )
        )
    return chapters


def _escape(text: str) -> str:
    """Escape ffmetadata's reserved characters."""
    for char in ("\\", "=", ";", "#"):
        text = text.replace(char, "\\" + char)
    return text.replace("\n", "\\\n")


def write_ffmetadata(path: Path, chapters: Sequence[Chapter]) -> Path:
    """Write an ffmetadata file containing only chapter definitions."""
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={chapter.start_ms}",
            f"END={chapter.end_ms}",
            f"title={_escape(chapter.title)}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cover_atom(cover_path: Path):
    from mutagen.mp4 import MP4Cover

    data = cover_path.read_bytes()
    suffix = cover_path.suffix.lower()
    if suffix == ".png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        fmt = MP4Cover.FORMAT_PNG
    else:
        fmt = MP4Cover.FORMAT_JPEG
    return MP4Cover(data, imageformat=fmt)


def apply_tags(target: Path, meta: Metadata) -> None:
    """Write tags, the audiobook flag and cover art onto a finished MP4.

    Always sets ``stik=2``; every other field is written only when non-empty,
    leaving the file otherwise untagged.
    """
    from mutagen.mp4 import MP4, MP4FreeForm
    from mutagen.mp4 import AtomDataType

    audio = MP4(str(target))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    tags["stik"] = [STIK_AUDIOBOOK]

    for attr, atom in _ATOMS.items():
        value = getattr(meta, attr, "").strip()
        if value:
            tags[atom] = [value]

    # Mirror the author into album artist so players group the book correctly.
    if meta.author.strip() and "aART" not in tags:
        tags["aART"] = [meta.author.strip()]
    # Without an album, players scatter chapters across the library.
    if not meta.album.strip() and meta.title.strip():
        tags["\xa9alb"] = [meta.title.strip()]

    for attr, atom in _FREEFORM.items():
        value = getattr(meta, attr, "").strip()
        if value:
            tags[atom] = [MP4FreeForm(value.encode("utf-8"), AtomDataType.UTF8)]

    if meta.cover_path is not None and Path(meta.cover_path).is_file():
        tags["covr"] = [_cover_atom(Path(meta.cover_path))]

    audio.save()
