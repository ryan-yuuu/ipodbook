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

import re
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


#: Source container tags -> Metadata fields. ffprobe normalises tag names
#: across containers, so one table serves MP3, FLAC and MP4 sources alike.
_SOURCE_TAGS = {
    "title": ("title",),
    "author": ("artist", "album_artist"),
    "narrator": ("composer",),
    "album": ("album",),
    "genre": ("genre",),
    "comment": ("comment",),
    "description": ("description",),
    "publisher": ("publisher",),
}

_NON_TEXT_FIELDS = {"cover_path", "cover_data", "track"}


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
    #: Cover image bytes, used in preference to ``cover_path``. Lets artwork
    #: inherited from a source file travel without a temporary file on disk.
    cover_data: bytes | None = None
    #: ``(number, total)`` -> ``trkn``, so players order a multi-volume book.
    track: tuple[int, int] | None = None

    def is_empty(self) -> bool:
        return not any(
            getattr(self, f.name) for f in fields(self) if f.name not in _NON_TEXT_FIELDS
        ) and self.cover_path is None and not self.cover_data

    def has_cover(self) -> bool:
        return bool(self.cover_data) or (
            self.cover_path is not None and Path(self.cover_path).is_file()
        )


def _first_tag(source: dict, names: tuple[str, ...]) -> str:
    lowered = {str(k).lower(): v for k, v in source.items()}
    for name in names:
        value = lowered.get(name)
        if value:
            return str(value).strip()
    return ""


def read_source_metadata(path: Path, *, cover: bool = True) -> Metadata:
    """Recover the tags a source file already carries.

    Merging parts of an existing audiobook means the title, author, narrator and
    artwork are already sitting in the files; this reads them back so they need
    not be retyped. Returns whatever was found -- absent tags stay blank.
    """
    from . import ffmpeg

    meta = Metadata()
    try:
        source = ffmpeg.probe(path, streams=False).get("format", {}).get("tags", {})
    except Exception:  # noqa: BLE001 - inheriting tags is a convenience, never fatal
        return meta

    for attr, names in _SOURCE_TAGS.items():
        setattr(meta, attr, _first_tag(source, names))

    # Dates arrive as "2010", "2010-06-01" or a full timestamp; keep the year.
    match = re.search(r"\d{4}", _first_tag(source, ("date", "year")))
    if match:
        meta.year = match.group(0)

    if cover:
        try:
            meta.cover_data = ffmpeg.extract_cover(path) or None
        except Exception:  # noqa: BLE001
            meta.cover_data = None
    return meta


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


def _cover_atom(data: bytes, suffix: str = ""):
    from mutagen.mp4 import MP4Cover

    if suffix.lower() == ".png" or data[:8] == b"\x89PNG\r\n\x1a\n":
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

    if meta.track is not None:
        tags["trkn"] = [(int(meta.track[0]), int(meta.track[1]))]

    if meta.cover_data:
        tags["covr"] = [_cover_atom(meta.cover_data)]
    elif meta.cover_path is not None and Path(meta.cover_path).is_file():
        path = Path(meta.cover_path)
        tags["covr"] = [_cover_atom(path.read_bytes(), path.suffix)]

    audio.save()
