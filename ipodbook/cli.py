"""Command line interface for ipodbook."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from .core import build, discover, ffmpeg, limits, measure, tags


def _bitrate(text: str) -> int:
    return int(text.lower().rstrip("k"))


def _volumes(text: str) -> int | None:
    """``auto`` means "as many as it takes"; a number pins the count."""
    if text.strip().lower() == "auto":
        return None
    count = int(text)
    if count < 1:
        raise argparse.ArgumentTypeError("volume count must be 1 or more")
    return count


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
        "--chapter-style", default="folder",
        choices=["filename", "folder", "number", "embedded"],
        help="how chapter titles are derived (default folder); 'embedded' takes "
             "the title each source file already carries, which is what you want "
             "when merging existing m4b files",
    )
    build_cmd.add_argument(
        "--volumes", type=_volumes, default=1, metavar="N|auto",
        help="split across N output files, or 'auto' for the fewest that fit the "
             "sample budget with room to spare (default 1)",
    )
    build_cmd.add_argument(
        "--inherit-tags", action="store_true",
        help="take title, author, narrator, year, description and cover art from "
             "the first source file that has them; explicit flags still win",
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

    if device.has_limit:
        durations = [t.seconds for t in tracks if t.ok]
        source_rates = {t.sample_rate for t in tracks if t.ok and t.sample_rate}
        # Encoding above the source rate cannot add detail, so the rate worth
        # planning against is the sources' own -- never higher.
        rate = min(source_rates) if source_rates else 22050
        try:
            groups = limits.plan_volumes(durations, rate, device.max_samples)
        except limits.CannotSplit as exc:
            print(f"\nCannot split at {rate} Hz: {exc}")
            return 0
        if len(groups) > 1:
            print(f"\nAt the source rate of {rate} Hz this needs {len(groups)} volumes:")
            for index, group in enumerate(groups, 1):
                span = sum(durations[i] for i in group)
                print(f"  Vol {index}: {len(group):>4} files  "
                      f"{limits.format_duration(span):>10}  "
                      f"{limits.usage_fraction(span, rate, device.max_samples) * 100:5.1f}% of budget")
            print("  Build with:  --volumes auto")
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
        volumes=args.volumes,
    )

    metadata = tags.Metadata()
    if args.inherit_tags:
        for track in tracks:
            metadata = tags.read_source_metadata(track.path)
            if not metadata.is_empty():
                print(f"Inherited tags from {track.path.name}", file=sys.stderr)
                break
    # Anything given explicitly overrides what the sources carried.
    for name in ("title", "author", "narrator", "album", "year", "genre",
                 "comment", "description", "synopsis", "publisher"):
        value = getattr(args, name)
        if value:
            setattr(metadata, name, value)
    if args.cover is not None:
        metadata.cover_path = args.cover
        metadata.cover_data = None

    state = {"phase": ""}

    def progress(phase: str, fraction: float, detail: str) -> None:
        if phase != state["phase"]:
            state["phase"] = phase
            print(file=sys.stderr)
        print(f"\r{phase:>8}: {fraction * 100:5.1f}%  {detail}   ",
              end="", file=sys.stderr, flush=True)

    try:
        results = build.build_volumes(
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
    device = settings.device
    print()
    if len(results) > 1:
        print(f"{len(results)} volumes written:")
    for result in results:
        print(f"{result.output}")
        print(f"  {limits.format_duration(result.duration_s)}  "
              f"{limits.format_size(result.size_bytes)}  "
              f"{result.chapters} chapters")
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
