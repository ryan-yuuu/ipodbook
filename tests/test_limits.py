"""Sample-budget feasibility and the volume split.

The partition is pure arithmetic and is the piece a mistake would quietly ruin:
an off-by-one puts a volume one sample over the limit and the iPod simply
refuses to play it, with nothing in the file to say why.
"""

from __future__ import annotations

import random

import pytest

from ipodbook.core import limits

HOUR = 3600.0
LIMIT = limits.INT32_MAX


def spans(durations, groups):
    return [sum(durations[i] for i in group) for group in groups]


class TestCapacity:
    def test_capacity_stays_under_the_strict_limit(self):
        # fits() is strict, so a volume of exactly max_samples must not qualify.
        capacity = limits.volume_capacity_s(22050, LIMIT)
        assert limits.fits(capacity, 22050, LIMIT)
        assert not limits.fits(LIMIT / 22050, 22050, LIMIT)

    def test_unlimited_device_has_infinite_capacity(self):
        assert limits.volume_capacity_s(44100, None) == float("inf")


class TestPlanVolumes:
    def test_short_book_stays_in_one_file(self):
        assert limits.plan_volumes([HOUR] * 5, 22050, LIMIT) == [[0, 1, 2, 3, 4]]

    def test_auto_split_leaves_headroom(self):
        # 52.7 h at 22.05 kHz is 195% of budget. Two volumes would technically
        # fit, at 96% and 99%; the automatic split must not settle for that.
        durations = [HOUR / 2] * 105
        groups = limits.plan_volumes(durations, 22050, LIMIT)
        peak = max(
            limits.usage_fraction(s, 22050, LIMIT) for s in spans(durations, groups)
        )
        assert len(groups) >= 3
        assert peak <= limits.SAFE_BUDGET_FRACTION

    def test_explicit_count_is_honoured(self):
        durations = [HOUR] * 40
        groups = limits.plan_volumes(durations, 22050, LIMIT, volumes=4)
        assert len(groups) == 4

    def test_explicit_count_that_cannot_fit_is_refused(self):
        with pytest.raises(limits.CannotSplit):
            limits.plan_volumes([HOUR] * 40, 44100, LIMIT, volumes=1)

    def test_groups_are_balanced(self):
        durations = [HOUR] * 40
        groups = limits.plan_volumes(durations, 22050, LIMIT, volumes=4)
        assert [len(g) for g in groups] == [10, 10, 10, 10]

    def test_split_falls_on_file_boundaries_in_order(self):
        durations = [HOUR] * 40
        groups = limits.plan_volumes(durations, 22050, LIMIT, volumes=4)
        assert [i for group in groups for i in group] == list(range(40))

    def test_single_file_over_budget_cannot_be_rescued(self):
        with pytest.raises(limits.CannotSplit, match="One file alone"):
            limits.plan_volumes([40 * HOUR], 22050, LIMIT)

    def test_single_file_between_soft_and_hard_limits_is_allowed(self):
        # 25 h exceeds the 85% target but fits the real 27.05 h ceiling, so it
        # must not be refused just because no split can improve on it.
        assert limits.plan_volumes([25 * HOUR], 22050, LIMIT) == [[0]]

    def test_unlimited_device_never_splits(self):
        assert limits.plan_volumes([HOUR] * 200, 44100, None) == [list(range(200))]

    def test_more_volumes_than_files_is_clamped(self):
        assert limits.plan_volumes([60.0, 60.0], 22050, LIMIT, volumes=9) == [[0], [1]]

    def test_empty_source_list_is_refused(self):
        with pytest.raises(limits.CannotSplit):
            limits.plan_volumes([], 22050, LIMIT)

    @pytest.mark.parametrize("seed", range(25))
    def test_every_plan_is_contiguous_complete_and_within_budget(self, seed):
        rng = random.Random(seed)
        durations = [rng.uniform(30, 4000) for _ in range(rng.randint(1, 80))]
        rate = rng.choice(limits.AAC_SAMPLE_RATES)
        try:
            groups = limits.plan_volumes(durations, rate, LIMIT)
        except limits.CannotSplit:
            return
        assert [i for g in groups for i in g] == list(range(len(durations)))
        for span in spans(durations, groups):
            assert limits.fits(span, rate, LIMIT)


class TestMinVolumes:
    def test_headroom_costs_volumes(self):
        durations = [HOUR] * 53
        tight = limits.min_volumes(durations, 22050, LIMIT)
        relaxed = limits.min_volumes(
            durations, 22050, LIMIT, headroom=limits.SAFE_BUDGET_FRACTION
        )
        assert tight == 2
        assert relaxed > tight

    def test_unlimited_device_needs_one(self):
        assert limits.min_volumes([HOUR] * 500, 44100, None) == 1
