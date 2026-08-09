"""Background threads, so the window never blocks on ffmpeg."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core import build, measure, tags
from ..core.measure import Cancelled, TrackInfo


class AnalyzeWorker(QThread):
    """Two-phase source analysis.

    Phase 1 reads container metadata, which is instant but approximate. Phase 2
    decodes each file to count its samples exactly. The UI shows a provisional
    budget after phase 1 so there is immediate feedback, then firms it up.
    """

    row_ready = Signal(int, object)      # index, TrackInfo
    phase_done = Signal(str, float)      # "quick" | "exact", total seconds
    progress = Signal(str, int, int)     # phase, done, total
    failed = Signal(str)

    def __init__(self, tracks: list[TrackInfo], parent=None) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        total = len(self._tracks)
        try:
            for index, track in enumerate(self._tracks):
                if self._cancel.is_set():
                    return
                probed = measure.probe_quick(track.path)
                track.seconds = probed.seconds
                track.sample_rate = probed.sample_rate
                track.channels = probed.channels
                track.codec = probed.codec
                track.error = probed.error
                track.exact = False
                self.row_ready.emit(index, track)
                self.progress.emit("quick", index + 1, total)
            if self._cancel.is_set():
                return
            self.phase_done.emit("quick", measure.total_seconds(self._tracks))

            index_of = {id(t): i for i, t in enumerate(self._tracks)}

            def on_progress(done: int, count: int) -> None:
                self.progress.emit("exact", done, count)

            measure.measure_all(
                self._tracks, progress=on_progress, cancel=self._cancel
            )
            if self._cancel.is_set():
                return
            for track in self._tracks:
                self.row_ready.emit(index_of[id(track)], track)
            self.phase_done.emit("exact", measure.total_seconds(self._tracks))
        except Cancelled:
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class BuildWorker(QThread):
    """Runs the pipeline and relays progress."""

    progress = Signal(str, float, str)   # phase, fraction, detail
    finished_ok = Signal(object)         # BuildResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        tracks: list[TrackInfo],
        output: Path,
        settings: build.Settings,
        metadata: tags.Metadata,
        *,
        overwrite: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._output = output
        self._settings = settings
        self._metadata = metadata
        self._overwrite = overwrite
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        try:
            result = build.build(
                self._tracks,
                self._output,
                self._settings,
                self._metadata,
                progress=lambda phase, fraction, detail: self.progress.emit(
                    phase, fraction, detail
                ),
                cancel=self._cancel,
                overwrite=self._overwrite,
            )
        except Cancelled:
            self.cancelled.emit()
        except build.BuildError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Unexpected error: {exc}")
        else:
            self.finished_ok.emit(result)
