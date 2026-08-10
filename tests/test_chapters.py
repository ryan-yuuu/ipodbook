"""Chapter titles and boundaries.

The embedded-title cases come from a real 117-file audiobook whose internal
chapter tables are mostly junk: every file carries one real chapter plus
zero-length markers naming its neighbours, and every title ends in a carriage
return.
"""

from __future__ import annotations

from pathlib import Path

from ipodbook.core import discover, tags
from ipodbook.core.measure import SourceChapter


def chapter(start, end, title):
    return SourceChapter(start=start, end=end, title=title)


class TestEmbeddedTitle:
    def test_reads_the_only_real_chapter(self):
        assert discover.embedded_title([chapter(0, 1310.9, "Chapter 1")]) == "Chapter 1"

    def test_ignores_zero_length_boundary_markers(self):
        # The shape every file in the sample book has.
        found = discover.embedded_title([
            chapter(0.0, 0.0, "Chapter 1"),
            chapter(0.0, 1028.179, "Chapter 2"),
            chapter(1028.179, 1028.180, "Chapter 3"),
        ])
        assert found == "Chapter 2"

    def test_strips_trailing_carriage_returns(self):
        assert discover.embedded_title([chapter(0, 600, "Chapter 47\r")]) == "Chapter 47"

    def test_longest_chapter_wins(self):
        found = discover.embedded_title([
            chapter(0, 30, "intro"),
            chapter(30, 900, "the real one"),
        ])
        assert found == "the real one"

    def test_no_chapters_yields_nothing(self):
        assert discover.embedded_title([]) == ""

    def test_all_degenerate_yields_nothing(self):
        assert discover.embedded_title([chapter(0, 0.1, "x"), chapter(0.1, 0.2, "y")]) == ""

    def test_blank_title_yields_nothing(self):
        assert discover.embedded_title([chapter(0, 600, "   ")]) == ""


class TestChapterTitle:
    path = Path("/books/Monte Cristo/part - 047.m4b")

    def test_embedded_style_prefers_the_stored_title(self):
        title = discover.chapter_title(
            self.path, "embedded", 46, chapters=[chapter(0, 900, "Chapter 47\r")]
        )
        assert title == "Chapter 47"

    def test_embedded_style_falls_back_to_the_filename(self):
        assert discover.chapter_title(self.path, "embedded", 46) == "part - 047"

    def test_embedded_style_falls_back_when_only_markers_exist(self):
        title = discover.chapter_title(
            self.path, "embedded", 46, chapters=[chapter(0, 0.001, "Chapter 47")]
        )
        assert title == "part - 047"

    def test_other_styles_ignore_embedded_chapters(self):
        stored = [chapter(0, 900, "Chapter 47")]
        assert discover.chapter_title(self.path, "number", 46, chapters=stored) == "Chapter 47"
        assert discover.chapter_title(self.path, "filename", 46, chapters=stored) == "part - 047"

    def test_folder_style_is_unchanged(self):
        assert discover.chapter_title(self.path, "folder", 0) == "Monte Cristo - part - 047"


class TestChapterBoundaries:
    def test_boundaries_are_laid_end_to_end(self):
        chapters = tags.chapters_from_durations([10.0, 5.0], ["a", "b"])
        assert [(c.start_ms, c.end_ms) for c in chapters] == [(0, 10000), (10000, 15000)]

    def test_rounding_does_not_accumulate(self):
        # 1/3 s per track: rounding each boundary independently would drift.
        count = 3000
        chapters = tags.chapters_from_durations([1 / 3] * count, ["x"] * count)
        assert chapters[-1].end_ms == round(count / 3 * 1000)

    def test_chapters_are_contiguous(self):
        chapters = tags.chapters_from_durations([1.1, 2.2, 3.3], list("abc"))
        for earlier, later in zip(chapters, chapters[1:]):
            assert earlier.end_ms == later.start_ms
