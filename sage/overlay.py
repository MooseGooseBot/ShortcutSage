"""PySide6 overlay for Shortcut Sage suggestions."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, cast

from PySide6.QtCore import SLOT, QEasingCurve, QPropertyAnimation, Qt, QTimer, Slot
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from sage.dbus_daemon import INTERFACE, OBJECT_PATH

logger = logging.getLogger(__name__)

try:
    from PySide6.QtDBus import QDBusConnection

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 ships QtDBus
    DBUS_AVAILABLE = False

# Identifiers the KWin script uses to find (and pin) the overlay window.
OVERLAY_WINDOW_TITLE = "Shortcut Sage Overlay"
OVERLAY_APP_ID = "shortcut-sage-overlay"

MAX_SUGGESTIONS = 3
DEFAULT_TIMEOUT_SECONDS = 8.0


class SuggestionChip(QWidget):
    """A chip displaying a single shortcut suggestion."""

    def __init__(self, key: str, description: str, priority: int):
        super().__init__()

        self.key = key
        self.description = description
        self.priority = priority

        self.setup_ui()

    def setup_ui(self) -> None:
        """Set up the UI for the chip."""
        self.setObjectName("suggestionChip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # Key label (the actual shortcut)
        self.key_label = QLabel(self.key)
        key_font = QFont()
        key_font.setBold(True)
        key_font.setPointSize(12)
        self.key_label.setFont(key_font)
        self.key_label.setStyleSheet("color: #4CAF50; background: transparent; border: none;")

        # Description label
        self.desc_label = QLabel(self.description)
        desc_font = QFont()
        desc_font.setPointSize(10)
        self.desc_label.setFont(desc_font)
        self.desc_label.setStyleSheet("color: white; background: transparent; border: none;")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumWidth(220)

        layout.addWidget(self.key_label)
        layout.addWidget(self.desc_label)

        # Scoped to the chip itself so the labels don't each get a border.
        self.setStyleSheet(
            """
            QWidget#suggestionChip {
                background-color: rgba(30, 30, 30, 0.9);
                border: 1px solid #555;
                border-radius: 6px;
            }
            """
        )


class OverlayWindow(QWidget):
    """Main overlay window that displays shortcut suggestions.

    The window is shown once and never unmapped again: on Wayland every map creates a
    new toplevel that the compositor may focus. "Hiding" therefore means clearing the
    chips and fading the content out, while the transparent, input-transparent window
    stays put.
    """

    def __init__(
        self,
        dbus_available: bool = True,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()

        self.dbus_available = dbus_available and DBUS_AVAILABLE
        self.timeout_seconds = timeout_seconds
        self.chips: list[SuggestionChip] = []
        self.fade_animation: QPropertyAnimation | None = None

        self.setup_window()
        self.setup_ui()

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.clear_suggestions)

        self.connect_dbus()

        logger.info(f"Overlay initialized (DBus: {self.dbus_available})")

    def setup_window(self) -> None:
        """Configure window properties for overlay."""
        self.setWindowTitle(OVERLAY_WINDOW_TITLE)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Top-left corner. Honoured on X11; on Wayland the KWin script places the
        # window (clients cannot position themselves there).
        self.setGeometry(20, 20, 300, 60)

    def setup_ui(self) -> None:
        """Set up the UI elements."""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)
        main_layout.setSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        self.setLayout(main_layout)

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity_effect)

        self.setStyleSheet("background-color: transparent;")

    def connect_dbus(self) -> None:
        """Subscribe to the daemon's Suggestions signal."""
        if not self.dbus_available:
            logger.info("Running overlay in fallback mode (no DBus)")
            return

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            logger.error("Cannot connect to the DBus session bus; overlay in fallback mode")
            self.dbus_available = False
            return

        # Match on path/interface (not the well-known name) so the subscription keeps
        # working across daemon restarts and when the daemon starts after the overlay.
        ok = bus.connect(  # type: ignore[call-overload]  # SLOT() is typed as str
            "",
            OBJECT_PATH,
            INTERFACE,
            "Suggestions",
            self,
            SLOT("on_suggestions(QString)"),
        )
        if not ok:
            logger.error("Failed to subscribe to Suggestions signal: %s", bus.lastError().message())
            self.dbus_available = False
            return
        logger.info("Listening for Shortcut Sage suggestions on DBus")

    @Slot(str)
    def on_suggestions(self, suggestions_json: str) -> None:
        """Handle incoming suggestions (JSON list) from DBus."""
        try:
            suggestions = json.loads(suggestions_json)
            if not isinstance(suggestions, list):
                raise ValueError("Suggestions payload must be a JSON list")
            self.update_suggestions(suggestions)
        except Exception as e:
            logger.error(f"Error processing suggestions: {e}")

    def update_suggestions(self, suggestions: list[dict[str, Any]]) -> None:
        """Replace the displayed suggestions (max 3)."""
        self._remove_chips()

        layout = self.layout()
        for suggestion in suggestions:
            if len(self.chips) >= MAX_SUGGESTIONS:
                break
            try:
                chip = SuggestionChip(
                    key=str(suggestion["key"]),
                    description=str(suggestion.get("description", "")),
                    priority=int(suggestion.get("priority", 0)),
                )
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                logger.warning("Skipping malformed suggestion %r: %s", suggestion, e)
                continue
            if layout is not None:
                layout.addWidget(chip)
            self.chips.append(chip)

        self.adjustSize()

        if self.chips:
            self.fade_in()
            if self.timeout_seconds > 0:
                self.hide_timer.start(int(self.timeout_seconds * 1000))
        else:
            self.hide_timer.stop()

    def _remove_chips(self) -> None:
        for chip in self.chips:
            chip.setParent(None)
            chip.deleteLater()
        self.chips.clear()

    def clear_suggestions(self) -> None:
        """Fade out and remove all chips (window stays mapped, fully transparent)."""
        self.hide_timer.stop()
        if not self.chips:
            return
        self.fade_out()

    def set_suggestions_fallback(self, suggestions: list[dict[str, Any]]) -> None:
        """Update suggestions when not using DBus (tests, demo, fallback)."""
        self.update_suggestions(suggestions)

    def _animate(self, start: float, end: float, on_finished: Any = None) -> None:
        if self.fade_animation is not None:
            self.fade_animation.stop()
        self.fade_animation = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.fade_animation.setDuration(250)
        self.fade_animation.setStartValue(start)
        self.fade_animation.setEndValue(end)
        self.fade_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        if on_finished is not None:
            self.fade_animation.finished.connect(on_finished)
        self.fade_animation.start()

    def fade_in(self) -> None:
        """Fade the content in."""
        self._animate(self.opacity_effect.opacity(), 1.0)

    def fade_out(self) -> None:
        """Fade the content out, then drop the chips."""

        def _done() -> None:
            self._remove_chips()
            self.adjustSize()

        self._animate(self.opacity_effect.opacity(), 0.0, _done)


DEMO_SUGGESTIONS = [
    {
        "action": "overview",
        "key": "Meta+W",
        "description": "Toggle Overview",
        "priority": 80,
    },
    {
        "action": "tile_left",
        "key": "Meta+Left",
        "description": "Tile window to left half",
        "priority": 60,
    },
]


def run_overlay(
    *,
    enable_dbus: bool = True,
    demo: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Launch the overlay UI."""
    QGuiApplication.setDesktopFileName(OVERLAY_APP_ID)  # Wayland app_id
    app = cast(QApplication, QApplication.instance() or QApplication([sys.argv[0]]))
    app.setApplicationName(OVERLAY_APP_ID)
    app.setApplicationDisplayName(OVERLAY_WINDOW_TITLE)
    app.setQuitOnLastWindowClosed(False)

    overlay = OverlayWindow(
        dbus_available=enable_dbus,
        timeout_seconds=0 if demo else timeout_seconds,
    )
    overlay.show()

    if demo:
        overlay.set_suggestions_fallback(DEMO_SUGGESTIONS)

    return app.exec()


def main() -> None:
    """Legacy CLI entry point for running directly."""
    demo = "--demo" in sys.argv
    sys.exit(run_overlay(enable_dbus=DBUS_AVAILABLE, demo=demo))


if __name__ == "__main__":
    main()
