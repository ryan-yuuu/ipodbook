"""Device sample-count limits and AAC sample-rate feasibility.

The governing constraint for old iPods is not file size or duration on its own,
but the total number of audio samples in the track:

    samples = duration_seconds * sample_rate

iPod firmware tracks playback position in a signed 32-bit integer, so a book
whose sample count reaches 2**31 fails to play. Empirically confirmed on an
iPod photo (A1099): 1.52e9 samples plays, 2.20e9 and 3.02e9 do not.

Bitrate does not appear in the equation and is therefore unconstrained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

#: Sample rates expressible in MPEG-4 AAC. The format stores the rate as a
#: 4-bit index into a fixed table, so values outside it (e.g. 30000) cannot be
#: encoded at all -- both ffmpeg's native encoder and Apple's reject them.
AAC_SAMPLE_RATES: tuple[int, ...] = (
    8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
)

#: Bitrates offered in the UI, in kbps.
BITRATES: tuple[int, ...] = (24, 32, 40, 48, 64, 80, 96, 128)

INT32_MAX = 2 ** 31  # 2,147,483,648


@dataclass(frozen=True)
class Device:
    """A playback target and the sample budget it imposes."""

    key: str
    label: str
    max_samples: int | None  # None means no known limit
    note: str

    @property
    def has_limit(self) -> bool:
        return self.max_samples is not None


DEVICES: tuple[Device, ...] = (
    Device(
        key="ipod",
        label="iPod (classic / photo / nano / mini)",
        max_samples=INT32_MAX,
        note=(
            "Firmware counts audio samples in a signed 32-bit integer. "
            "Duration x sample rate must stay under 2,147,483,648."
        ),
    ),
    Device(
        key="unlimited",
        label="No limit (modern players)",
        max_samples=None,
        note="Phones, computers and modern players impose no practical sample limit.",
    ),
)

DEFAULT_DEVICE = DEVICES[0]


def device_by_key(key: str) -> Device:
    for device in DEVICES:
        if device.key == key:
            return device
    raise KeyError(f"unknown device: {key!r}")


def sample_count(duration_s: float, sample_rate: int) -> int:
    """Total samples a track of this duration occupies at this rate."""
    return int(round(duration_s * sample_rate))


def max_duration_s(sample_rate: int, max_samples: int | None) -> float | None:
    """Longest book this rate allows, or None when the device has no limit."""
    if max_samples is None:
        return None
    return max_samples / sample_rate


def fits(duration_s: float, sample_rate: int, max_samples: int | None) -> bool:
    if max_samples is None:
        return True
    return sample_count(duration_s, sample_rate) < max_samples


def usage_fraction(duration_s: float, sample_rate: int, max_samples: int | None) -> float:
    """Share of the sample budget consumed. 1.0 is exactly at the limit."""
    if max_samples is None:
        return 0.0
    return sample_count(duration_s, sample_rate) / max_samples


def feasible_rates(duration_s: float, max_samples: int | None) -> tuple[int, ...]:
    """AAC rates that keep this duration inside the budget, ascending."""
    return tuple(r for r in AAC_SAMPLE_RATES if fits(duration_s, r, max_samples))


def best_rate(duration_s: float, max_samples: int | None) -> int | None:
    """Highest AAC rate that fits, i.e. the most bandwidth available."""
    usable = feasible_rates(duration_s, max_samples)
    return usable[-1] if usable else None


class CannotSplit(ValueError):
    """No arrangement of these files fits the budget."""


def volume_capacity_s(sample_rate: int, max_samples: int | None) -> float:
    """Longest a single volume may run, in seconds.

    One sample short of the device limit, because ``fits`` is strict: a track of
    exactly ``max_samples`` does not play.
    """
    if max_samples is None:
        return float("inf")
    return (max_samples - 1) / sample_rate


def _pack(durations: Sequence[float], capacity: float) -> list[list[int]]:
    """Greedily group consecutive indices without exceeding ``capacity``.

    Order is fixed -- these are chapters of one book -- so first-fit in order
    also yields the fewest possible groups.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    load = 0.0
    for index, duration in enumerate(durations):
        if current and load + duration > capacity:
            groups.append(current)
            current, load = [index], duration
        else:
            current.append(index)
            load += duration
    if current:
        groups.append(current)
    return groups


def _balanced(durations: Sequence[float], count: int) -> list[list[int]]:
    """Split into at most ``count`` groups, minimising the longest one.

    Binary search on capacity. The groups are taken from the last *feasible*
    probe rather than recomputed from the converged capacity, so a rounding
    error at the boundary can never yield more groups than asked for.
    """
    low, high = max(durations), sum(durations)
    best = _pack(durations, high)
    for _ in range(64):
        middle = (low + high) / 2
        groups = _pack(durations, middle)
        if len(groups) <= count:
            best, high = groups, middle
        else:
            low = middle
    return best


#: Share of the budget an automatically chosen split aims to stay under.
#: Packing to the bare minimum number of volumes leaves them sitting at 96-99%
#: of the limit, which is no place to be when the whole point of the tool is
#: margin. This is the same threshold at which the budget meter turns amber.
SAFE_BUDGET_FRACTION = 0.85


def min_volumes(
    durations: Sequence[float],
    sample_rate: int,
    max_samples: int | None,
    *,
    headroom: float = 1.0,
) -> int:
    """Fewest volumes this book can be split into and still play.

    ``headroom`` below 1.0 packs against a fraction of the budget, trading extra
    volumes for margin.
    """
    if max_samples is None:
        return 1
    capacity = volume_capacity_s(sample_rate, max_samples) * headroom
    return len(_pack(durations, capacity))


def plan_volumes(
    durations: Sequence[float],
    sample_rate: int,
    max_samples: int | None,
    *,
    volumes: int | None = None,
) -> list[list[int]]:
    """Group file indices into volumes that each fit the sample budget.

    ``volumes=None`` picks the fewest that fit inside ``SAFE_BUDGET_FRACTION``
    and then balances them, so no volume sits needlessly close to the limit.
    An explicit count is honoured as long as every volume still plays. Splits
    fall on file boundaries; a single file longer than the budget cannot be
    rescued by splitting.
    """
    if not durations:
        raise CannotSplit("No source files to split.")
    capacity = volume_capacity_s(sample_rate, max_samples)

    if volumes is None:
        if max_samples is None:
            return [list(range(len(durations)))]
        longest = max(durations)
        if longest > capacity:
            raise CannotSplit(
                f"One file alone runs {format_duration(longest)}, over the "
                f"{format_duration(capacity)} a volume may hold at "
                f"{sample_rate / 1000:g} kHz. Use a lower sample rate."
            )
        volumes = min_volumes(
            durations, sample_rate, max_samples, headroom=SAFE_BUDGET_FRACTION
        )

    volumes = max(1, min(volumes, len(durations)))
    groups = _balanced(durations, volumes)

    if max_samples is not None:
        for index, group in enumerate(groups, 1):
            total = sum(durations[i] for i in group)
            if not fits(total, sample_rate, max_samples):
                raise CannotSplit(
                    f"Volume {index} would run {format_duration(total)}, over the "
                    f"{format_duration(capacity)} limit at {sample_rate / 1000:g} kHz. "
                    f"Use more volumes or a lower sample rate."
                )
    return groups


def estimated_size_bytes(duration_s: float, bitrate_kbps: int, sample_rate: int) -> int:
    """Approximate output size: audio payload plus the MP4 sample-size index.

    ``stsz`` carries one 4-byte entry per AAC frame, and each frame covers 1024
    samples -- so the index grows with sample rate but is unaffected by bitrate.
    """
    audio = duration_s * bitrate_kbps * 1000 / 8
    frames = duration_s * sample_rate / 1024
    index = frames * 4
    return int(audio + index + 300_000)  # + moov overhead and cover art


def nyquist_hz(sample_rate: int) -> int:
    """Highest frequency a rate can represent: half the sample rate."""
    return sample_rate // 2


def describe_rate(sample_rate: int, duration_s: float, max_samples: int | None) -> str:
    """One-line explanation shown beneath the sample-rate control."""
    khz = sample_rate / 1000
    nyq = nyquist_hz(sample_rate) / 1000
    limit = max_duration_s(sample_rate, max_samples)
    if limit is None:
        return f"{khz:g} kHz -> {nyq:g} kHz bandwidth"
    return f"{khz:g} kHz -> {nyq:g} kHz bandwidth - max {limit / 3600:.1f} h on this device"


def format_samples(n: int) -> str:
    """Compact sample count, e.g. 1.65B."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    return f"{n:,}"


def format_duration(seconds: float) -> str:
    """H:MM:SS, or M:SS below an hour."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(num_bytes: int) -> str:
    mib = num_bytes / 1024 / 1024
    if mib >= 1024:
        return f"{mib / 1024:.2f} GiB"
    return f"{mib:.0f} MiB"
