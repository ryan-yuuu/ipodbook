# ipodbook

Build M4B audiobooks from loose audio files, without silently producing one your
iPod refuses to play.

## Why this exists

Old iPods track playback position in a **signed 32-bit sample counter**. A book
fails to play once the `sample count > 2**31`

    duration_seconds x sample_rate  >=  2**31   (2,147,483,648)

File size and bitrate are irrelevant to this; only duration and sample rate
matter. Confirmed on an iPod photo (A1099):

| Sample count | Rate | Result |
|---|---|---|
| 1,516,150,535 | 22.05 kHz | plays |
| 2,200,309,824 | 32 kHz | fails |
| 3,018,156,941 | 44.1 kHz | fails |

ipodbook makes that budget visible while you choose settings, and refuses to
build a file that would exceed it.

## Install

Requires `ffmpeg` and `ffprobe` on your PATH.

```bash
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Debian/Ubuntu

cd ipodbook
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python -m ipodbook              # GUI
.venv/bin/python -m ipodbook cli --help   # CLI
```

## GUI

Drag in files or a folder (folders are scanned recursively). The list order is
playback order; reorder by dragging or with the arrow buttons.

The **sample budget** meter beneath the panels updates as you change the sample
rate. Rates that cannot fit the current book are disabled with the reason
attached, and Build stays greyed out with an explanation until everything is
valid. Durations are estimated instantly, then measured exactly in the
background -- the meter is drawn dashed until measurement finishes.

All metadata is optional and omitted when blank. The audiobook flag (`stik=2`),
which is what makes a player remember your position, is always written.

## CLI

```bash
# Which sample rates fit?
ipodbook cli plan ./discs

# Build
ipodbook cli build ./discs --out book.m4b \
    --rate 24000 --bitrate 40 \
    --title "The Time Machine" --author "H. G. Wells"
```

`plan` prints every AAC rate with its sample count and budget usage, then names
the highest rate that fits.

## Choosing settings

**Sample rate** sets bandwidth: half the rate is the highest frequency kept
(Nyquist). Narration needs about 11 kHz to sound natural, so 22.05 kHz is a good
default and 24 kHz is the highest rate that fits a 19-hour book on an iPod.
Only the rates in the MPEG-4 AAC table exist -- 30 kHz cannot be encoded at all.

| Rate | Bandwidth | Max book length under 2^31 |
|---|---|---|
| 44.1 kHz | 22 kHz | 13.5 h |
| 32 kHz | 16 kHz | 18.6 h |
| 24 kHz | 12 kHz | 24.8 h |
| 22.05 kHz | 11 kHz | 27.0 h |
| 16 kHz | 8 kHz | 37.3 h |

**Bitrate** affects quality and file size only. It has no effect on the sample
budget, so raise it freely. Above roughly 48 kbps mono you are mostly preserving
artifacts already present in a typical MP3 source.

**Encoder** -- Apple's AudioToolbox encoder is slightly more efficient at a given
bitrate. Both produce AAC-LC; ipodbook never emits HE-AAC, which pre-2007 iPods
cannot decode, and verification rejects the output if it somehow appears.

## How it builds

Each source file is decoded to headerless PCM and piped into a single encoder
process. This is deliberate and load-bearing:

* **ffmpeg's concat demuxer is never used.** It derives its timeline from
  container metadata, and MP3s without a Xing header only carry a duration
  *estimate*. Concatenating 227 such files produced 1,630 non-monotonic DTS
  warnings and silently dropped five minutes of audio. Raw PCM has no
  timestamps, so nothing can drift.
* **Durations are measured, not read.** Chapter boundaries come from decoding
  each file and counting samples. Container estimates ran ~0.5% short on real CD
  rips, which compounds to minutes across a full book.
* **Tags are written by mutagen, not ffmpeg.** ffmpeg's MP4 muxer silently drops
  freeform iTunes atoms such as `publisher`, and the `use_metadata_tags` flag
  that preserves them discards cover art instead. Writing chapters with ffmpeg
  and everything else with mutagen avoids the tradeoff.
* **Output is verified before delivery.** Codec, AAC-LC profile, sample rate,
  channels, duration, chapter count, sample budget, faststart and the 32-bit
  track header are all checked. The file is assembled under a temporary name and
  moved into place only on success, so a failed or cancelled build never leaves
  a partial `.m4b`.

## Layout

```
ipodbook/
  core/          GUI-agnostic; shared by CLI and GUI
    limits.py    device budgets, AAC rate table, feasibility math
    discover.py  file scanning, natural sort, chapter titles
    measure.py   exact duration measurement by decoding
    tags.py      ffmetadata chapters + mutagen tag writing
    build.py     the pipeline
    verify.py    post-build checks
    ffmpeg.py    binary discovery, probing, encoder detection
  gui/           PySide6 window, widgets, worker threads
  cli.py
```
