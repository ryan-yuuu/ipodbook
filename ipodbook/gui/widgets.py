"""Reusable widgets for the ipodbook window."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

# Bar colours by how much of the sample budget is consumed.
_GREEN = "#2f9e44"
_AMBER = "#e8a13a"
_RED = "#d84a3f"

#: Opacity of de-emphasised text, matching the ~60% macOS uses for secondary
#: labels. Applied to the theme's own text colour rather than to a fixed grey.
_MUTED_ALPHA = 150


def muted_css() -> str:
    """A ``color:`` declaration for secondary text that survives dark mode.

    Qt's ``palette(mid)`` is a bevel-shading role, not a text role: it is a dark
    grey in every theme, so it reads correctly against a light window and turns
    illegible against a dark one. Fading the theme's actual text colour instead
    de-emphasises the same amount in either direction.
    """
    colour = QApplication.palette().color(QPalette.WindowText)
    return f"color: rgba({colour.red()}, {colour.green()}, {colour.blue()}, {_MUTED_ALPHA});"


class HelpLabel(QLabel):
    """Small explanatory line shown beneath a control.

    Explanations live permanently under their control rather than in hover
    tooltips, which are undiscoverable and useless on first run.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self._restyling = False
        self._restyle()

    def _restyle(self) -> None:
        # setStyleSheet itself posts a PaletteChange back to this widget, so
        # without the guard restyling would call itself forever.
        if self._restyling:
            return
        self._restyling = True
        try:
            self.setStyleSheet(f"{muted_css()} font-size: 11px;")
        finally:
            self._restyling = False

    def changeEvent(self, event) -> None:  # noqa: N802
        # Follow the system switching between light and dark while running.
        # Only the application-level event is a real theme change; the
        # per-widget one also fires for our own restyling.
        if event.type() == QEvent.ApplicationPaletteChange:
            self._restyle()
        super().changeEvent(event)


class SectionTitle(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setStyleSheet("font-weight: 600;")


class BudgetMeter(QFrame):
    """Live gauge of the device sample budget.

    This is the safety rail of the whole application: it turns the abstract
    "duration x sample rate must stay under 2^31" rule into something you can
    watch move as you change settings.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self._title = SectionTitle("Sample budget")
        self._verdict = QLabel("")
        self._verdict.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(self._verdict)
        layout.addLayout(header)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(14)
        layout.addWidget(self._bar)

        self._detail = HelpLabel("Add files to see how much of the budget they use.")
        layout.addWidget(self._detail)

        self.set_idle()

    def _paint(self, colour: str, dashed: bool = False) -> None:
        border = f"1px {'dashed' if dashed else 'solid'} palette(mid)"
        self._bar.setStyleSheet(
            f"QProgressBar {{ border: {border}; border-radius: 7px; "
            f"background: palette(base); }}"
            f"QProgressBar::chunk {{ background: {colour}; border-radius: 6px; }}"
        )

    def set_idle(self) -> None:
        self._bar.setValue(0)
        self._verdict.setText("")
        self._paint(_GREEN)
        self._detail.setText("Add files to see how much of the budget they use.")

    def set_unlimited(self, samples: int, duration_text: str) -> None:
        from ..core import limits

        self._bar.setValue(0)
        self._verdict.setText("no limit")
        self._verdict.setStyleSheet(muted_css())
        self._paint(_GREEN)
        self._detail.setText(
            f"{duration_text} - {limits.format_samples(samples)} samples. "
            f"This target imposes no sample limit."
        )

    def update_budget(
        self,
        *,
        samples: int,
        max_samples: int,
        duration_text: str,
        provisional: bool,
        note: str = "",
    ) -> None:
        from ..core import limits

        fraction = samples / max_samples
        percent = int(round(fraction * 100))
        self._bar.setValue(min(percent, 100))

        if fraction >= 1.0:
            colour, verdict = _RED, "over limit"
        elif fraction >= 0.85:
            colour, verdict = _AMBER, "close to limit"
        else:
            colour, verdict = _GREEN, "fits"
        self._paint(colour, dashed=provisional)
        self._verdict.setText(f"{verdict}  -  {percent}%")
        self._verdict.setStyleSheet(f"color: {colour}; font-weight: 600;")

        kind = "estimated" if provisional else "measured"
        text = (
            f"{duration_text} ({kind}) - {limits.format_samples(samples)} of "
            f"{limits.format_samples(max_samples)} samples."
        )
        if note:
            text += f"  {note}"
        self._detail.setText(text)


class CollapsibleSection(QWidget):
    """A titled section that can be folded away.

    Used to keep optional metadata out of the way without hiding that it exists.
    """

    toggled_open = Signal(bool)

    def __init__(self, title: str, *, expanded: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._button = QToolButton()
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(expanded)
        self._button.setStyleSheet("QToolButton { border: none; font-weight: 600; }")
        self._button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._button.clicked.connect(self._on_click)
        layout.addWidget(self._button)

        self._content = QWidget()
        self._content.setVisible(expanded)
        self._content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(self._content)

        self.body = QVBoxLayout(self._content)
        self.body.setContentsMargins(16, 2, 0, 6)
        self.body.setSpacing(8)

    def _on_click(self, checked: bool) -> None:
        self._content.setVisible(checked)
        self._button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.toggled_open.emit(checked)

    def set_summary(self, text: str) -> None:
        self._button.setToolTip(text)
