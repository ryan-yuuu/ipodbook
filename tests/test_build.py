"""Volume naming, per-volume tags, and destination safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from ipodbook.core import build, tags
from ipodbook.core.measure import TrackInfo


class TestVolumePath:
    def test_single_volume_keeps_the_chosen_name(self):
        assert build.volume_path(Path("/out/Book.m4b"), 1, 1) == Path("/out/Book.m4b")

    def test_several_volumes_are_numbered(self):
        assert build.volume_path(Path("/out/Book.m4b"), 2, 3).name == "Book - Vol 2 of 3.m4b"

    def test_numbering_sorts_in_reading_order(self):
        names = [build.volume_path(Path("/o/B.m4b"), i, 3).name for i in (1, 2, 3)]
        assert names == sorted(names)

    def test_the_suffix_is_preserved(self):
        assert build.volume_path(Path("/o/B.m4b"), 1, 2).suffix == ".m4b"


class TestVolumeMetadata:
    output = Path("/out/Monte Cristo.m4b")
    base = tags.Metadata(title="The Count of Monte Cristo", author="Alexandre Dumas")

    def test_single_volume_is_untouched(self):
        assert build._volume_metadata(self.base, self.output, 1, 1) is self.base

    def test_volumes_share_an_album_and_differ_by_title(self):
        first = build._volume_metadata(self.base, self.output, 1, 3)
        second = build._volume_metadata(self.base, self.output, 2, 3)
        assert first.album == second.album == "The Count of Monte Cristo"
        assert first.title == "The Count of Monte Cristo - Vol 1 of 3"
        assert second.title == "The Count of Monte Cristo - Vol 2 of 3"

    def test_track_numbers_order_the_volumes(self):
        assert build._volume_metadata(self.base, self.output, 2, 3).track == (2, 3)

    def test_the_original_is_not_mutated(self):
        build._volume_metadata(self.base, self.output, 2, 3)
        assert self.base.title == "The Count of Monte Cristo"
        assert self.base.track is None

    def test_an_explicit_album_is_kept(self):
        meta = tags.Metadata(title="Book", album="Collected Works")
        assert build._volume_metadata(meta, self.output, 1, 2).album == "Collected Works"

    def test_untitled_books_fall_back_to_the_output_name(self):
        volume = build._volume_metadata(tags.Metadata(), self.output, 1, 2)
        assert volume.title == "Monte Cristo - Vol 1 of 2"


class TestCheckDestination:
    @staticmethod
    def sources(*paths):
        return [TrackInfo(path=p) for p in paths]

    def test_a_source_file_cannot_also_be_the_output(self, tmp_path):
        source = tmp_path / "part1.m4b"
        source.write_bytes(b"audio")
        with pytest.raises(build.BuildError, match="also one of the source files"):
            build._check_destination(source, self.sources(source), overwrite=True)

    def test_the_guard_sees_through_relative_paths(self, tmp_path):
        source = tmp_path / "part1.m4b"
        source.write_bytes(b"audio")
        indirect = tmp_path / "sub" / ".." / "part1.m4b"
        (tmp_path / "sub").mkdir()
        with pytest.raises(build.BuildError, match="also one of the source files"):
            build._check_destination(indirect, self.sources(source), overwrite=True)

    def test_a_separate_destination_is_fine(self, tmp_path):
        source = tmp_path / "part1.m4b"
        source.write_bytes(b"audio")
        build._check_destination(tmp_path / "book.m4b", self.sources(source), overwrite=False)

    def test_an_existing_output_needs_overwrite(self, tmp_path):
        target = tmp_path / "book.m4b"
        target.write_bytes(b"old")
        with pytest.raises(build.BuildError, match="already exists"):
            build._check_destination(target, self.sources(), overwrite=False)
        build._check_destination(target, self.sources(), overwrite=True)

    def test_a_missing_folder_is_reported(self, tmp_path):
        with pytest.raises(build.BuildError, match="does not exist"):
            build._check_destination(tmp_path / "nope" / "b.m4b", self.sources(), overwrite=False)


class TestMetadata:
    def test_inherited_cover_counts_as_content(self):
        assert tags.Metadata().is_empty()
        assert not tags.Metadata(cover_data=b"\xff\xd8\xff").is_empty()

    def test_a_track_number_alone_is_not_content(self):
        # trkn is bookkeeping the splitter adds, not something the user typed.
        assert tags.Metadata(track=(1, 3)).is_empty()

    def test_has_cover_accepts_either_source(self, tmp_path):
        assert not tags.Metadata().has_cover()
        assert tags.Metadata(cover_data=b"x").has_cover()
        image = tmp_path / "cover.jpg"
        image.write_bytes(b"x")
        assert tags.Metadata(cover_path=image).has_cover()
