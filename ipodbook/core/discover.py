"""Finding audio files and putting them in the order a listener expects."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from .measure import SourceChapter

#: Extensions we hand to ffmpeg. MP3 is the common case for ripped audiobooks,
#: but the decode step is codec-agnostic so anything ffmpeg reads works.
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac",
    ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff", ".ape", ".wv",
})

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> list:
    """Sort key that orders embedded numbers numerically.

    Plain lexicographic sorting puts "disc 10" before "disc 2", which silently
    scrambles a multi-disc audiobook. This splits digit runs out and compares
    them as integers.
    """
    parts = _DIGITS.split(text)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _path_sort_key(path: Path) -> list:
    """Order by directory components first, then filename -- all naturally."""
    key: list = []
    for part in path.parts[:-1]:
        key.extend(natural_key(part))
    key.extend(natural_key(path.name))
    return key


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def scan(paths: list[str | Path], *, recursive: bool = True) -> list[Path]:
    """Collect audio files from a mix of files and directories.

    Directories are walked recursively. Results are de-duplicated and returned
    in natural order, so ``disc 2`` precedes ``disc 10`` and ``track2`` precedes
    ``track10``. Hidden files (``._`` AppleDouble stubs, ``.DS_Store``) are skipped.
    """
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            for child in walker:
                if child.is_file() and is_audio(child) and not child.name.startswith("."):
                    found.add(child.resolve())
        elif path.is_file() and is_audio(path):
            found.add(path.resolve())
    return sorted(found, key=_path_sort_key)


def sort_naturally(paths: list[Path]) -> list[Path]:
    """Re-apply natural ordering to an existing list."""
    return sorted(paths, key=_path_sort_key)


#: Embedded chapters shorter than this are treated as markers, not chapters.
#: Splitters that cut a book into per-chapter files routinely leave zero-length
#: entries at both edges naming the neighbouring file's chapter -- on a real
#: 117-file audiobook, 187 of 304 embedded entries were of this kind.
MIN_EMBEDDED_CHAPTER_S = 1.0


def embedded_title(chapters: Sequence["SourceChapter"]) -> str:
    """Title of the embedded chapter that actually covers a file, or "".

    Picks the longest real chapter rather than the first, so the boundary
    markers described above cannot win. Titles are stripped, which also removes
    the trailing carriage returns some taggers leave behind.
    """
    real = [c for c in chapters if c.seconds >= MIN_EMBEDDED_CHAPTER_S]
    if not real:
        return ""
    return max(real, key=lambda c: c.seconds).title.strip()


def chapter_title(
    path: Path,
    style: str,
    index: int,
    *,
    root: Path | None = None,
    chapters: Sequence["SourceChapter"] | None = None,
) -> str:
    """Derive a chapter title from a file path.

    Styles:
      ``filename``  -- "track01"
      ``folder``    -- "disc 1 - track01"  (parent folder prefix; disambiguates
                       multi-disc rips where track names repeat per disc)
      ``number``    -- "Chapter 1"
      ``embedded``  -- the title the file already carries ("Chapter 47"),
                       falling back to ``filename`` when it has none
    """
    if style == "number":
        return f"Chapter {index + 1}"
    if style == "embedded":
        title = embedded_title(chapters or ())
        if title:
            return title
        style = "filename"
    stem = path.stem
    if style == "folder":
        parent = path.parent.name
        if root is not None and path.parent == root:
            parent = ""
        if parent:
            return f"{parent} - {stem}"
    return stem


def common_root(paths: list[Path]) -> Path | None:
    """Deepest directory containing every path, for trimming display names."""
    if not paths:
        return None
    parents = [p.parent for p in paths]
    root = parents[0]
    for parent in parents[1:]:
        while root != parent and root not in parent.parents:
            if root.parent == root:
                return root
            root = root.parent
    return root


def display_name(path: Path, root: Path | None) -> str:
    """Path shown in the file list -- relative to the common root when useful."""
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return path.name
