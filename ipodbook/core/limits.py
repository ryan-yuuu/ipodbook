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
