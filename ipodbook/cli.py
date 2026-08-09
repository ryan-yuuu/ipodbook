"""Command line interface for ipodbook."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from .core import build, discover, ffmpeg, limits, measure, tags


def _bitrate(text: str) -> int:
    return int(text.lower().rstrip("k"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipodbook",
        description="Build an iPod-compatible M4B audiobook from audio files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="build an audiobook")
    build_cmd.add_argument(
        "sources", nargs="+", type=Path,
        help="audio files and/or folders (folders are scanned recursively)",
    )
    build_cmd.add_argument("-o", "--out", type=Path, required=True, help="output .m4b path")
    build_cmd.add_argument(
        "--rate", type=int, default=22050,
        help=f"output sample rate (default 22050); one of {', '.join(map(str, limits.AAC_SAMPLE_RATES))}",
    )
    build_cmd.add_argument("--bitrate", type=_bitrate, default=40, help="kbps (default 40)")
    build_cmd.add_argument("--stereo", action="store_true", help="keep stereo (default mono)")
    build_cmd.add_argument("--encoder", default=None, help="aac_at or aac (default: best available)")
    build_cmd.add_argument(
        "--device", default="ipod", choices=[d.key for d in limits.DEVICES],
        help="playback target enforcing the sample budget (default ipod)",
    )
    build_cmd.add_argument("--no-chapters", action="store_true", help="omit chapter markers")
    build_cmd.add_argument(
        "--chapter-style", default="folder", choices=["filename", "folder", "number"],
        help="how chapter titles are derived (default folder)",
    )
    build_cmd.add_argument("--force", action="store_true", help="overwrite an existing output")

    for name in ("title", "author", "narrator", "album", "year", "genre", "comment",
                 "description", "synopsis", "publisher"):
        build_cmd.add_argument(f"--{name}", default="", help=f"optional {name} tag")
    build_cmd.add_argument("--cover", type=Path, default=None, help="cover image (jpg/png)")

    plan = sub.add_parser("plan", help="report duration and which sample rates fit")
    plan.add_argument("sources", nargs="+", type=Path)
    plan.add_argument(
        "--device", default="ipod", choices=[d.key for d in limits.DEVICES],
    )
    plan.add_argument("--quick", action="store_true", help="use estimates instead of decoding")

    return parser


def _collect(sources: list[Path]) -> list[Path]:
    files = discover.scan(sources)
    if not files:
        raise SystemExit("No audio files found.")
    return files


def _measure(files: list[Path], *, quick: bool) -> list[measure.TrackInfo]:
    tracks = [measure.probe_quick(p) for p in files]
    if quick:
        return tracks

    def report(done: int, total: int) -> None:
        print(f"\rMeasuring {done}/{total}", end="", file=sys.stderr, flush=True)

    measure.measure_all(tracks, progress=report)
    print(file=sys.stderr)
    return tracks


def _cmd_plan(args) -> int:
    files = _collect(args.sources)
    tracks = _measure(files, quick=args.quick)
    total = measure.total_seconds(tracks)
    device = limits.device_by_key(args.device)
    kind = "estimated" if args.quick else "measured"

    print(f"{len(files)} files - {limits.format_duration(total)} ({kind})")
    print(f"Target: {device.label}")
    if device.has_limit:
        print(f"Budget: {limits.format_samples(device.max_samples)} samples\n")
    print(f"{'rate':>8}  {'samples':>9}  {'usage':>7}  fits")
    for rate in limits.AAC_SAMPLE_RATES:
        count = limits.sample_count(total, rate)
        usage = limits.usage_fraction(total, rate, device.max_samples)
        fits = limits.fits(total, rate, device.max_samples)
        print(
            f"{rate:>8}  {limits.format_samples(count):>9}  "
            f"{usage * 100 if device.has_limit else 0:>6.1f}%  {'yes' if fits else 'NO'}"
        )
    best = limits.best_rate(total, device.max_samples)
    if best:
        print(f"\nHighest rate that fits: {best} Hz ({limits.nyquist_hz(best) / 1000:g} kHz bandwidth)")
    else:
        print("\nNo AAC sample rate fits this duration on this device.")
    return 0


def _cmd_build(args) -> int:
    encoders = ffmpeg.available_encoders()
    if not encoders:
        raise SystemExit("No AAC encoder available in this ffmpeg build.")
    encoder = args.encoder or encoders[0]

    files = _collect(args.sources)
    tracks = _measure(files, quick=False)

    settings = build.Settings(
        sample_rate=args.rate,
        bitrate_kbps=args.bitrate,
        channels=2 if args.stereo else 1,
        encoder=encoder,
        device_key=args.device,
        chapters=not args.no_chapters,
        chapter_style=args.chapter_style,
    )
    metadata = tags.Metadata(
        title=args.title, author=args.author, narrator=args.narrator,
        album=args.album, year=args.year, genre=args.genre,
        comment=args.comment, description=args.description,
        synopsis=args.synopsis, publisher=args.publisher,
        cover_path=args.cover,
    )

    state = {"phase": ""}

    def progress(phase: str, fraction: float, detail: str) -> None:
        if phase != state["phase"]:
            state["phase"] = phase
            print(file=sys.stderr)
        print(f"\r{phase:>8}: {fraction * 100:5.1f}%  {detail}   ",
              end="", file=sys.stderr, flush=True)

    try:
        result = build.build(
            tracks, args.out, settings, metadata,
            progress=progress, overwrite=args.force,
        )
    except build.BuildError as exc:
        print(file=sys.stderr)
        raise SystemExit(f"error: {exc}")
    except measure.Cancelled:
        print(file=sys.stderr)
        raise SystemExit("cancelled")

    print(file=sys.stderr)
    print(f"\n{result.output}")
    print(f"  {limits.format_duration(result.duration_s)}  "
          f"{limits.format_size(result.size_bytes)}  "
          f"{result.chapters} chapters")
    device = settings.device
    if device.has_limit:
        usage = result.samples / device.max_samples * 100
        print(f"  {limits.format_samples(result.samples)} samples "
              f"({usage:.0f}% of the {device.label} limit)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not ffmpeg.have_ffmpeg():
        raise SystemExit("ffmpeg and ffprobe are required. Install with: brew install ffmpeg")
    if args.command == "plan":
        return _cmd_plan(args)
    return _cmd_build(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
