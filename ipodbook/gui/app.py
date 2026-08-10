"""The ipodbook main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..core import build, discover, ffmpeg, limits, measure, tags
from ..core.measure import TrackInfo
from .widgets import BudgetMeter, CollapsibleSection, HelpLabel, SectionTitle
from .workers import AnalyzeWorker, BuildWorker

WINDOW_TITLE = "ipodbook"


class FileTable(QTableWidget):
    """Track list. Order here is playback order."""

    def __init__(self, on_dropped, parent=None) -> None:
        super().__init__(0, 3, parent)
        self._on_dropped = on_dropped
        self.setHorizontalHeaderLabels(["#", "File", "Length"])
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setAcceptDrops(True)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)

    # -- accept files dragged in from the file manager ----------------------
    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self._on_dropped(paths)
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1020, 720)

        self.tracks: list[TrackInfo] = []
        self.output_path: Path | None = None
        self._built: list[Path] = []
        self.cover_path: Path | None = None
        self.inherited_cover: bytes | None = None
        self._analyzer: AnalyzeWorker | None = None
        self._builder: BuildWorker | None = None
        self._exact = False
        self._rate_wanted: int | None = None
        self._encoders = ffmpeg.available_encoders()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_source_panel())
        splitter.addWidget(self._build_settings_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)

        self.meter = BudgetMeter()
        root.addWidget(self.meter)
        root.addWidget(self._build_action_bar())

        self._refresh_rates()
        self._update_budget()
        self._update_build_enabled()

        if not ffmpeg.have_ffmpeg():
            self._warn_missing_ffmpeg()

    # ------------------------------------------------------------------ UI
    def _build_source_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)

        layout.addWidget(SectionTitle("Source files"))
        layout.addWidget(HelpLabel(
            "Drag files or folders here, or use the buttons. "
            "The order in this list is the order they play."
        ))

        self.table = FileTable(self._add_paths)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for label, slot in (
            ("Add Files…", self._choose_files),
            ("Add Folder…", self._choose_folder),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        for label, slot, tip in (
            ("↑", self._move_up, "Move selected up"),
            ("↓", self._move_down, "Move selected down"),
            ("Remove", self._remove_selected, "Remove selected files"),
            ("Sort", self._sort_naturally, "Sort by folder and filename, numerically"),
            ("Clear", self._clear_files, "Remove all files"),
        ):
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        layout.addLayout(buttons)

        self.summary_label = HelpLabel("No files yet.")
        layout.addWidget(self.summary_label)
        return panel

    def _build_settings_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(12)

        # --- output --------------------------------------------------------
        output_box = QGroupBox("Output")
        output_layout = QVBoxLayout(output_box)
        row = QHBoxLayout()
        self.output_button = QPushButton("Save As…")
        self.output_button.clicked.connect(self._choose_output)
        self.output_label = QLabel("No destination chosen")
        self.output_label.setWordWrap(True)
        row.addWidget(self.output_button)
        row.addWidget(self.output_label, 1)
        output_layout.addLayout(row)

        output_layout.addSpacing(6)
        output_layout.addWidget(QLabel("Volumes"))
        self.volumes_combo = QComboBox()
        self.volumes_combo.addItem("Single file", 1)
        self.volumes_combo.addItem("Split to fit the budget", None)
        for count in range(2, 9):
            self.volumes_combo.addItem(f"{count} volumes", count)
        self.volumes_combo.currentIndexChanged.connect(self._on_volumes_changed)
        output_layout.addWidget(self.volumes_combo)
        self.volumes_help = HelpLabel()
        output_layout.addWidget(self.volumes_help)
        layout.addWidget(output_box)

        # --- target --------------------------------------------------------
        target_box = QGroupBox("Target device")
        target_layout = QVBoxLayout(target_box)
        self.device_combo = QComboBox()
        for device in limits.DEVICES:
            self.device_combo.addItem(device.label, device.key)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        target_layout.addWidget(self.device_combo)
        self.device_help = HelpLabel(limits.DEFAULT_DEVICE.note)
        target_layout.addWidget(self.device_help)
        layout.addWidget(target_box)

        # --- audio ---------------------------------------------------------
        audio_box = QGroupBox("Audio")
        audio_layout = QVBoxLayout(audio_box)
        audio_layout.setSpacing(4)

        self.rate_combo = QComboBox()
        self.rate_combo.currentIndexChanged.connect(self._on_audio_changed)
        # ``activated`` fires only for real user interaction, unlike
        # ``currentIndexChanged``, which also fires when we rebuild the list.
        self.rate_combo.activated.connect(self._on_rate_chosen)
        audio_layout.addWidget(QLabel("Sample rate"))
        audio_layout.addWidget(self.rate_combo)
        self.rate_help = HelpLabel()
        audio_layout.addWidget(self.rate_help)

        self.bitrate_combo = QComboBox()
        for kbps in limits.BITRATES:
            self.bitrate_combo.addItem(f"{kbps} kbps", kbps)
        self.bitrate_combo.setCurrentIndex(limits.BITRATES.index(40))
        self.bitrate_combo.currentIndexChanged.connect(self._on_audio_changed)
        audio_layout.addSpacing(6)
        audio_layout.addWidget(QLabel("Bitrate"))
        audio_layout.addWidget(self.bitrate_combo)
        self.bitrate_help = HelpLabel()
        audio_layout.addWidget(self.bitrate_help)

        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Mono", 1)
        self.channel_combo.addItem("Stereo", 2)
        self.channel_combo.currentIndexChanged.connect(self._on_audio_changed)
        audio_layout.addSpacing(6)
        audio_layout.addWidget(QLabel("Channels"))
        audio_layout.addWidget(self.channel_combo)
        audio_layout.addWidget(HelpLabel(
            "Spoken word is almost always mono. Mono halves the file size at the "
            "same quality and does not affect the sample budget."
        ))

        self.encoder_combo = QComboBox()
        for name in self._encoders:
            self.encoder_combo.addItem(ffmpeg.encoder_label(name), name)
        if not self._encoders:
            self.encoder_combo.addItem("none available", "")
            self.encoder_combo.setEnabled(False)
        audio_layout.addSpacing(6)
        audio_layout.addWidget(QLabel("Encoder"))
        audio_layout.addWidget(self.encoder_combo)
        audio_layout.addWidget(HelpLabel(
            "Apple's AudioToolbox encoder is slightly more efficient at a given "
            "bitrate. Both produce AAC-LC, the only AAC old iPods can decode."
        ))
        layout.addWidget(audio_box)

        # --- chapters ------------------------------------------------------
        chapter_box = QGroupBox("Chapters")
        chapter_layout = QVBoxLayout(chapter_box)
        self.chapters_check = QCheckBox("One chapter per file")
        self.chapters_check.setChecked(True)
        self.chapters_check.toggled.connect(self._on_chapters_toggled)
        chapter_layout.addWidget(self.chapters_check)
        self.chapter_style_combo = QComboBox()
        self.chapter_style_combo.addItem("Folder and filename", "folder")
        self.chapter_style_combo.addItem("Filename", "filename")
        self.chapter_style_combo.addItem("Chapter 1, 2, 3…", "number")
        self.chapter_style_combo.addItem("Title stored in the file", "embedded")
        chapter_layout.addWidget(self.chapter_style_combo)
        chapter_layout.addWidget(HelpLabel(
            "Chapter boundaries come from each file's measured length, not from "
            "its metadata, so they land exactly on the track transitions. "
            "\"Title stored in the file\" reuses the name an m4b already carries "
            "and falls back to the filename when there is none."
        ))
        layout.addWidget(chapter_box)

        # --- metadata ------------------------------------------------------
        meta_section = CollapsibleSection("Metadata (optional)")
        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight)
        self.meta_fields: dict[str, QLineEdit] = {}
        for key, label in (
            ("title", "Title"),
            ("author", "Author"),
            ("narrator", "Narrator"),
            ("album", "Album"),
            ("year", "Year"),
            ("genre", "Genre"),
            ("publisher", "Publisher"),
        ):
            edit = QLineEdit()
            edit.setPlaceholderText("leave blank to omit")
            self.meta_fields[key] = edit
            form.addRow(label, edit)
        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText("short blurb — leave blank to omit")
        self.description_edit.setFixedHeight(56)
        form.addRow("Description", self.description_edit)
        self.synopsis_edit = QTextEdit()
        self.synopsis_edit.setPlaceholderText("long blurb — leave blank to omit")
        self.synopsis_edit.setFixedHeight(56)
        form.addRow("Synopsis", self.synopsis_edit)
        meta_section.body.addWidget(form_host)

        cover_row = QHBoxLayout()
        self.cover_button = QPushButton("Choose Cover…")
        self.cover_button.clicked.connect(self._choose_cover)
        self.cover_clear = QPushButton("Clear")
        self.cover_clear.clicked.connect(self._clear_cover)
        self.cover_label = QLabel("none")
        cover_row.addWidget(self.cover_button)
        cover_row.addWidget(self.cover_clear)
        cover_row.addWidget(self.cover_label, 1)
        meta_section.body.addLayout(cover_row)

        inherit_row = QHBoxLayout()
        self.inherit_button = QPushButton("Read from Source Files")
        self.inherit_button.setToolTip(
            "Fill these fields from the tags the source files already carry"
        )
        self.inherit_button.clicked.connect(self._inherit_metadata)
        inherit_row.addWidget(self.inherit_button)
        inherit_row.addStretch(1)
        meta_section.body.addLayout(inherit_row)

        meta_section.body.addWidget(HelpLabel(
            "Every field here is optional and left out of the file when blank. "
            "The audiobook flag that makes players remember your position is "
            "always written. When merging existing audiobooks, \"Read from "
            "Source Files\" recovers the title, author, narrator and cover art "
            "the files already hold."
        ))
        layout.addWidget(meta_section)

        layout.addStretch(1)
        return scroll

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Add source files to begin.")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.progress.setFixedWidth(220)

        self.reveal_button = QPushButton("Show in Folder")
        self.reveal_button.setVisible(False)
        self.reveal_button.clicked.connect(self._reveal_output)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_build)

        self.build_button = QPushButton("Build Audiobook")
        self.build_button.setDefault(True)
        self.build_button.clicked.connect(self._start_build)

        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.reveal_button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.build_button)
        return bar

    # -------------------------------------------------------------- sources
    def _choose_files(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(discover.AUDIO_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add audio files", "", f"Audio ({patterns});;All files (*)"
        )
        if paths:
            self._add_paths([Path(p) for p in paths])

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder")
        if folder:
            self._add_paths([Path(folder)])

    def _add_paths(self, paths: list[Path]) -> None:
        found = discover.scan(paths)
        if not found:
            QMessageBox.information(
                self, "No audio found",
                "None of those items contained audio files ipodbook recognises.",
            )
            return
        existing = {t.path for t in self.tracks}
        added = [TrackInfo(path=p) for p in found if p not in existing]
        if not added:
            self.status_label.setText("Those files are already in the list.")
            return
        self.tracks.extend(added)
        lookup = {t.path: t for t in self.tracks}
        self.tracks = [lookup[p] for p in discover.sort_naturally(list(lookup))]
        self._rebuild_table()
        self._suggest_output()
        self._start_analysis()

    def _rebuild_table(self) -> None:
        root = discover.common_root([t.path for t in self.tracks])
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.tracks))
        for row, track in enumerate(self.tracks):
            self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            name = QTableWidgetItem(discover.display_name(track.path, root))
            name.setToolTip(str(track.path))
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, QTableWidgetItem(self._length_text(track)))
        self.table.blockSignals(False)
        self._update_summary()
        self._update_build_enabled()

    @staticmethod
    def _length_text(track: TrackInfo) -> str:
        if track.error:
            return "error"
        if not track.seconds:
            return "—"
        text = limits.format_duration(track.seconds)
        return text if track.exact else f"~{text}"

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def _move_up(self) -> None:
        rows = self._selected_rows()
        if not rows or rows[0] == 0:
            return
        for row in rows:
            self.tracks[row - 1], self.tracks[row] = self.tracks[row], self.tracks[row - 1]
        self._rebuild_table()
        self._select_rows([r - 1 for r in rows])

    def _move_down(self) -> None:
        rows = self._selected_rows()
        if not rows or rows[-1] >= len(self.tracks) - 1:
            return
        for row in reversed(rows):
            self.tracks[row + 1], self.tracks[row] = self.tracks[row], self.tracks[row + 1]
        self._rebuild_table()
        self._select_rows([r + 1 for r in rows])

    def _select_rows(self, rows: list[int]) -> None:
        self.table.clearSelection()
        for row in rows:
            if 0 <= row < self.table.rowCount():
                self.table.selectRow(row)

    def _remove_selected(self) -> None:
        rows = set(self._selected_rows())
        if not rows:
            return
        self.tracks = [t for i, t in enumerate(self.tracks) if i not in rows]
        self._rebuild_table()
        self._refresh_rates()
        self._update_budget()

    def _sort_naturally(self) -> None:
        lookup = {t.path: t for t in self.tracks}
        self.tracks = [lookup[p] for p in discover.sort_naturally(list(lookup))]
        self._rebuild_table()

    def _clear_files(self) -> None:
        self._stop_analysis()
        self.tracks = []
        self._exact = False
        self._rebuild_table()
        self.meter.set_idle()
        self.status_label.setText("Add source files to begin.")

    def _update_summary(self) -> None:
        if not self.tracks:
            self.summary_label.setText("No files yet.")
            return
        total = measure.total_seconds(self.tracks)
        kind = "" if self._exact else " (estimated)"
        detail = measure.source_summary(self.tracks)
        suffix = f" - {detail}" if detail else ""
        self.summary_label.setText(
            f"{len(self.tracks)} files - {limits.format_duration(total)}{kind}{suffix}"
        )

    # ------------------------------------------------------------- analysis
    def _start_analysis(self) -> None:
        self._stop_analysis()
        self._exact = False
        if not self.tracks:
            return
        self._analyzer = AnalyzeWorker(list(self.tracks), self)
        self._analyzer.row_ready.connect(self._on_row_ready)
        self._analyzer.phase_done.connect(self._on_phase_done)
        self._analyzer.progress.connect(self._on_analyze_progress)
        self._analyzer.failed.connect(self._on_analyze_failed)
        self._analyzer.start()

    def _stop_analysis(self) -> None:
        if self._analyzer is not None:
            self._analyzer.cancel()
            self._analyzer.wait(3000)
            self._analyzer = None

    def _on_row_ready(self, index: int, track: TrackInfo) -> None:
        if 0 <= index < self.table.rowCount():
            self.table.setItem(index, 2, QTableWidgetItem(self._length_text(track)))

    def _on_analyze_progress(self, phase: str, done: int, total: int) -> None:
        word = "Reading" if phase == "quick" else "Measuring"
        self.status_label.setText(f"{word} {done}/{total} files…")

    def _on_phase_done(self, phase: str, total_seconds: float) -> None:
        self._exact = phase == "exact"
        self._update_summary()
        self._refresh_rates()
        self._update_budget()
        self._update_build_enabled()
        if self._exact:
            self.status_label.setText("Ready.")
            self._analyzer = None

    def _on_analyze_failed(self, message: str) -> None:
        self.status_label.setText("Could not read some files.")
        QMessageBox.warning(self, "Analysis failed", message)

    # ------------------------------------------------------------- settings
    def _device(self) -> limits.Device:
        return limits.device_by_key(self.device_combo.currentData())

    def _rate(self) -> int:
        data = self.rate_combo.currentData()
        return int(data) if data else 22050

    def _volumes(self) -> int | None:
        return self.volumes_combo.currentData()

    def _plan(self) -> list[list[int]] | None:
        """Volume split for the current settings, or None if there isn't one."""
        durations = [t.seconds for t in self.tracks if t.ok]
        if not durations:
            return None
        # Provisional durations run low, so plan against the padded figure --
        # otherwise a book that only just fits gains a volume after measuring.
        scale = 1.0 if self._exact else 1.01
        try:
            return limits.plan_volumes(
                [d * scale for d in durations],
                self._rate(),
                self._device().max_samples,
                volumes=self._volumes(),
            )
        except limits.CannotSplit:
            return None

    def _total_seconds(self) -> float:
        return measure.total_seconds(self.tracks)

    def _budget_seconds(self) -> float:
        """Duration used for feasibility checks.

        Container estimates ran low on real CD rips, so provisional numbers
        carry a 1% margin. That stops a book which only just fits from being
        reported as safe before it has actually been measured.
        """
        total = self._total_seconds()
        return total if self._exact else total * 1.01

    def _on_device_changed(self) -> None:
        self.device_help.setText(self._device().note)
        self._refresh_rates()
        self._update_budget()
        self._update_build_enabled()

    def _on_audio_changed(self) -> None:
        self._update_budget()
        self._update_build_enabled()

    def _on_rate_chosen(self) -> None:
        self._rate_wanted = self._rate()

    def _on_volumes_changed(self) -> None:
        # Splitting changes which rates are reachable, so the rate list has to
        # be rebuilt as well as the meter.
        self._refresh_rates()
        self._update_budget()
        self._update_build_enabled()

    def _on_chapters_toggled(self, checked: bool) -> None:
        self.chapter_style_combo.setEnabled(checked)

    def _rate_usable(self, rate: int, seconds: float) -> bool:
        """Whether this rate can produce a playable result.

        With splitting enabled the whole book no longer has to fit in one file,
        so a rate is usable as long as *some* arrangement of volumes works.
        """
        if seconds <= 0:
            return True
        device = self._device()
        if self._volumes() == 1:
            return limits.fits(seconds, rate, device.max_samples)
        longest = max((t.seconds for t in self.tracks if t.ok), default=0.0)
        return longest <= limits.volume_capacity_s(rate, device.max_samples)

    def _source_rate(self) -> int:
        """Lowest sample rate present in the sources, or 0 if unknown."""
        rates = {t.sample_rate for t in self.tracks if t.ok and t.sample_rate}
        return min(rates) if rates else 0

    def _preferred_rate(self, seconds: float) -> int:
        """The rate to select when the user has not chosen one.

        Encoding above the sources' own rate cannot recover detail that is not
        there -- it only inflates the file and spends sample budget on an empty
        frequency band -- so the sources set the ceiling. Below that, take the
        highest rate the current volume settings can actually deliver.
        """
        usable = [r for r in limits.AAC_SAMPLE_RATES if self._rate_usable(r, seconds)]
        if not usable:
            return limits.AAC_SAMPLE_RATES[0]
        ceiling = self._source_rate()
        within = [r for r in usable if r <= ceiling] if ceiling else []
        return within[-1] if within else usable[-1]

    def _refresh_rates(self) -> None:
        """Rebuild the rate list, disabling anything that cannot fit."""
        device = self._device()
        seconds = self._budget_seconds()
        splitting = self._volumes() != 1

        self.rate_combo.blockSignals(True)
        self.rate_combo.clear()
        model = self.rate_combo.model()
        for rate in limits.AAC_SAMPLE_RATES:
            usable = self._rate_usable(rate, seconds)
            label = f"{rate / 1000:g} kHz"
            if seconds > 0 and device.has_limit:
                if not usable:
                    label += "  -  too long"
                elif splitting and not limits.fits(seconds, rate, device.max_samples):
                    volumes = limits.min_volumes(
                        [t.seconds for t in self.tracks if t.ok], rate,
                        device.max_samples, headroom=limits.SAFE_BUDGET_FRACTION,
                    )
                    label += f"  -  {volumes} volumes"
                else:
                    usage = limits.usage_fraction(seconds, rate, device.max_samples)
                    label += f"  -  {usage * 100:.0f}% of budget"
            self.rate_combo.addItem(label, rate)
            if not usable:
                item = model.item(self.rate_combo.count() - 1)
                if item is not None:
                    item.setEnabled(False)

        # A rate the user picked is honoured whenever it works, and remembered
        # while it does not -- so a rate that only a split can deliver comes
        # back when splitting is re-enabled, rather than being lost the moment
        # a single-file setting invalidates it.
        if self._rate_wanted and self._rate_usable(self._rate_wanted, seconds):
            wanted = self._rate_wanted
        else:
            wanted = self._preferred_rate(seconds) if seconds > 0 else 22050
        self.rate_combo.setCurrentIndex(max(0, self.rate_combo.findData(wanted)))
        self.rate_combo.blockSignals(False)

    def _update_budget(self) -> None:
        device = self._device()
        seconds = self._total_seconds()
        rate = self._rate()
        bitrate = int(self.bitrate_combo.currentData() or 40)

        self.rate_help.setText(
            limits.describe_rate(rate, self._budget_seconds(), device.max_samples)
            + f". Half this value ({limits.nyquist_hz(rate) / 1000:g} kHz) is the "
            "highest frequency kept."
        )
        if seconds > 0:
            size = limits.estimated_size_bytes(seconds, bitrate, rate)
            self.bitrate_help.setText(
                f"About {limits.format_size(size)}. Bitrate affects quality and file "
                "size only — it has no effect on the sample budget."
            )
        else:
            self.bitrate_help.setText(
                "Bitrate affects quality and file size only — it has no effect on "
                "the sample budget."
            )

        self._update_volumes_help()

        if seconds <= 0:
            self.meter.set_idle()
            return

        samples = limits.sample_count(seconds, rate)
        duration_text = limits.format_duration(seconds)
        if not device.has_limit:
            self.meter.set_unlimited(samples, duration_text)
            return

        # When the book is being split, the number that matters is the longest
        # volume, not the whole book -- a 195%-of-budget bar would be nonsense
        # for a plan that produces three perfectly legal files.
        plan = self._plan()
        if plan is not None and len(plan) > 1:
            durations = [t.seconds for t in self.tracks if t.ok]
            spans = [sum(durations[i] for i in group) for group in plan]
            longest = max(spans)
            self.meter.update_budget(
                samples=limits.sample_count(longest, rate),
                max_samples=device.max_samples,
                duration_text=limits.format_duration(longest),
                provisional=not self._exact,
                note=f"Longest of {len(plan)} volumes; {duration_text} in total.",
            )
            return

        note = ""
        if not limits.fits(self._budget_seconds(), rate, device.max_samples):
            best = limits.best_rate(self._budget_seconds(), device.max_samples)
            note = f"Use {best / 1000:g} kHz or lower, or split into volumes." if best else \
                "Too long for any rate; split into volumes."
        self.meter.update_budget(
            samples=samples,
            max_samples=device.max_samples,
            duration_text=duration_text,
            provisional=not self._exact,
            note=note,
        )

    def _update_volumes_help(self) -> None:
        """Describe the split the current settings would produce."""
        if not self.tracks:
            self.volumes_help.setText(
                "Split a book that is too long for one file across several."
            )
            return
        plan = self._plan()
        if plan is None:
            self.volumes_help.setText(
                "No split fits at this sample rate. Choose a lower rate."
            )
            return
        if len(plan) == 1:
            self.volumes_help.setText("One file.")
            return
        durations = [t.seconds for t in self.tracks if t.ok]
        rate, device = self._rate(), self._device()
        spans = [sum(durations[i] for i in group) for group in plan]
        peak = max(limits.usage_fraction(s, rate, device.max_samples) for s in spans)
        text = (
            f"{len(plan)} files, the longest {limits.format_duration(max(spans))} "
            f"at {peak * 100:.0f}% of the budget."
        )
        if self.output_path is not None:
            example = build.volume_path(self.output_path, 1, len(plan))
            text += f"  Named \"{example.name}\"…"
        self.volumes_help.setText(text)

    def _update_build_enabled(self) -> None:
        busy = self._builder is not None and self._builder.isRunning()
        reasons = []
        if not self.tracks:
            reasons.append("add source files")
        if self.output_path is None:
            reasons.append("choose a destination")
        if not self._encoders:
            reasons.append("install ffmpeg")
        seconds = self._budget_seconds()
        if seconds > 0 and not self._rate_usable(self._rate(), seconds):
            reasons.append("sample budget exceeded")
        elif seconds > 0 and self.tracks and self._plan() is None:
            reasons.append("no volume split fits")
        self.build_button.setEnabled(not reasons and not busy)
        if busy:
            return
        if reasons:
            self.status_label.setText("To build: " + ", ".join(reasons) + ".")
        elif self.status_label.text().startswith("To build:"):
            # Clear our own stale blocker, but leave any other message alone --
            # otherwise changing a setting would wipe the result of a build.
            self.status_label.setText("Ready." if self._exact else "")

    # --------------------------------------------------------------- output
    def _suggest_output(self) -> None:
        if self.output_path is not None or not self.tracks:
            return
        root = discover.common_root([t.path for t in self.tracks])
        name = (root.name if root else self.tracks[0].path.stem) or "audiobook"
        parent = root.parent if root else self.tracks[0].path.parent
        self.output_path = parent / f"{name}.m4b"
        self._show_output()

    def _choose_output(self) -> None:
        start = str(self.output_path) if self.output_path else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save audiobook as", start, "Audiobook (*.m4b)"
        )
        if path:
            if not path.lower().endswith(".m4b"):
                path += ".m4b"
            self.output_path = Path(path)
            self._show_output()
            self._update_build_enabled()

    def _show_output(self) -> None:
        if self.output_path is None:
            self.output_label.setText("No destination chosen")
            return
        self.output_label.setText(str(self.output_path))
        self.output_label.setToolTip(str(self.output_path))
        self._update_volumes_help()

    def _choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", "", "Images (*.jpg *.jpeg *.png)"
        )
        if path:
            self.cover_path = Path(path)
            self.cover_label.setText(self.cover_path.name)

    def _clear_cover(self) -> None:
        self.cover_path = None
        self.inherited_cover = None
        self.cover_label.setText("none")

    # ---------------------------------------------------------------- build
    def _metadata(self) -> tags.Metadata:
        meta = tags.Metadata(cover_path=self.cover_path)
        for key, edit in self.meta_fields.items():
            setattr(meta, key, edit.text().strip())
        meta.description = self.description_edit.toPlainText().strip()
        meta.synopsis = self.synopsis_edit.toPlainText().strip()
        # A chosen cover file always wins over one inherited from the sources.
        if self.cover_path is None and self.inherited_cover:
            meta.cover_data = self.inherited_cover
        return meta

    def _inherit_metadata(self) -> None:
        """Fill the metadata form from the tags the sources already carry."""
        if not self.tracks:
            QMessageBox.information(
                self, "No source files",
                "Add the audiobook files first, then read their tags.",
            )
            return
        for track in self.tracks:
            found = tags.read_source_metadata(track.path)
            if found.is_empty():
                continue
            for key, edit in self.meta_fields.items():
                value = getattr(found, key, "")
                if value:
                    edit.setText(value)
            if found.description:
                self.description_edit.setPlainText(found.description)
            if found.synopsis:
                self.synopsis_edit.setPlainText(found.synopsis)
            if found.cover_data:
                self.inherited_cover = found.cover_data
                if self.cover_path is None:
                    self.cover_label.setText(
                        f"from {track.path.name} ({len(found.cover_data) // 1024} KiB)"
                    )
            self.status_label.setText(f"Read tags from {track.path.name}.")
            return
        QMessageBox.information(
            self, "No tags found",
            "None of the source files carry tags ipodbook can read.",
        )

    def _settings(self) -> build.Settings:
        return build.Settings(
            sample_rate=self._rate(),
            bitrate_kbps=int(self.bitrate_combo.currentData() or 40),
            channels=int(self.channel_combo.currentData() or 1),
            encoder=str(self.encoder_combo.currentData() or "aac"),
            device_key=self._device().key,
            chapters=self.chapters_check.isChecked(),
            chapter_style=str(self.chapter_style_combo.currentData() or "folder"),
            volumes=self._volumes(),
        )

    def _start_build(self) -> None:
        if self.output_path is None or not self.tracks:
            return
        plan = self._plan()
        volumes = len(plan) if plan else 1
        existing = [
            build.volume_path(self.output_path, i, volumes)
            for i in range(1, volumes + 1)
        ]
        existing = [p for p in existing if p.exists()]
        overwrite = False
        if existing:
            names = "\n".join(f"  {p.name}" for p in existing[:5])
            if len(existing) > 5:
                names += f"\n  …and {len(existing) - 5} more"
            answer = QMessageBox.question(
                self, "Replace file?",
                f"These already exist:\n\n{names}\n\nReplace them?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            overwrite = True

        self._stop_analysis()
        self.reveal_button.setVisible(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.cancel_button.setVisible(True)
        self.build_button.setEnabled(False)

        self._builder = BuildWorker(
            list(self.tracks), self.output_path, self._settings(), self._metadata(),
            overwrite=overwrite, parent=self,
        )
        self._builder.progress.connect(self._on_build_progress)
        self._builder.finished_ok.connect(self._on_build_ok)
        self._builder.failed.connect(self._on_build_failed)
        self._builder.cancelled.connect(self._on_build_cancelled)
        self._builder.start()

    def _on_build_progress(self, phase: str, fraction: float, detail: str) -> None:
        words = {
            build.PHASE_MEASURE: "Measuring",
            build.PHASE_ENCODE: "Encoding",
            build.PHASE_TAG: "Writing metadata",
            build.PHASE_VERIFY: "Verifying",
        }
        self.status_label.setText(f"{words.get(phase, phase)} — {detail}")
        self.progress.setValue(int(fraction * 100))

    def _finish_build_ui(self) -> None:
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self._builder = None
        self._update_build_enabled()

    def _on_build_ok(self, results: list[build.BuildResult]) -> None:
        self._finish_build_ui()
        device = self._device()
        self._built = [r.output for r in results]
        if len(results) == 1:
            result = results[0]
            parts = [
                limits.format_duration(result.duration_s),
                limits.format_size(result.size_bytes),
                f"{result.chapters} chapters",
            ]
            if device.has_limit:
                parts.append(f"{result.samples / device.max_samples * 100:.0f}% of budget")
            self.status_label.setText(f"Built {result.output.name} — " + ", ".join(parts))
        else:
            span = limits.format_duration(sum(r.duration_s for r in results))
            size = limits.format_size(sum(r.size_bytes for r in results))
            chapters = sum(r.chapters for r in results)
            self.status_label.setText(
                f"Built {len(results)} volumes — {span}, {size}, {chapters} chapters"
            )
        self.reveal_button.setVisible(True)
        # A build measures every track exactly, so the meter is no longer provisional.
        self._exact = True
        self._update_summary()
        self._update_budget()

    def _on_build_failed(self, message: str) -> None:
        self._finish_build_ui()
        self.status_label.setText("Build failed.")
        QMessageBox.critical(self, "Build failed", message)

    def _on_build_cancelled(self) -> None:
        self._finish_build_ui()
        self.status_label.setText("Build cancelled. No file was written.")

    def _cancel_build(self) -> None:
        if self._builder is not None:
            self._builder.cancel()
            self.status_label.setText("Cancelling…")

    def _reveal_output(self) -> None:
        target = next((p for p in self._built if p.exists()), self.output_path)
        if target is None or not target.exists():
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))

    # ----------------------------------------------------------------- misc
    def _warn_missing_ffmpeg(self) -> None:
        QMessageBox.warning(
            self, "ffmpeg not found",
            "ipodbook needs ffmpeg and ffprobe.\n\nInstall them with:\n"
            "    brew install ffmpeg\n\nThen restart ipodbook.",
        )

    def closeEvent(self, event):  # noqa: N802
        self._stop_analysis()
        if self._builder is not None and self._builder.isRunning():
            self._builder.cancel()
            self._builder.wait(5000)
        super().closeEvent(event)


def run() -> int:
    QGuiApplication.setApplicationDisplayName(WINDOW_TITLE)
    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    window = MainWindow()
    window.show()
    return app.exec()
