"""End-to-End demonstration of Shortcut Sage pipeline."""

import json
import logging
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from sage.dbus_daemon import Daemon
from sage.overlay import OverlayWindow


def create_demo_config() -> Path:
    """Create demo configuration files."""
    # Create temporary directory for demo configs
    config_dir = Path(tempfile.mkdtemp(prefix="shortcut_sage_demo_"))

    # Create shortcuts config
    shortcuts_content = """
version: "1.0"
shortcuts:
  - key: "Meta+D"
    action: "show_desktop"
    description: "Show desktop"
    category: "desktop"
  - key: "Meta+W"
    action: "overview"
    description: "Show application overview"
    category: "desktop"
  - key: "Meta+Left"
    action: "tile_left"
    description: "Tile window to left half"
    category: "window"
  - key: "Meta+Right"
    action: "tile_right"
    description: "Tile window to right half"
    category: "window"
"""

    # Create rules config
    rules_content = """
version: "1.0"
rules:
  - name: "after_show_desktop"
    context:
      type: "event_sequence"
      pattern: ["show_desktop"]
      window: 3
    suggest:
      - action: "overview"
        priority: 80
      - action: "tile_left"
        priority: 60
    cooldown: 300
  - name: "after_tile_left"
    context:
      type: "event_sequence"
      pattern: ["tile_left"]
      window: 5
    suggest:
      - action: "tile_right"
        priority: 85
      - action: "overview"
        priority: 60
    cooldown: 180
"""

    (config_dir / "shortcuts.yaml").write_text(shortcuts_content)
    (config_dir / "rules.yaml").write_text(rules_content)

    return config_dir


def run_demo() -> None:
    """Run the end-to-end demo."""
    print("Shortcut Sage - End-to-End Demo")
    print("=" * 40)

    # Setup logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Create demo configuration
    config_dir = create_demo_config()
    print(f"Created demo config in: {config_dir}")

    # Create daemon in fallback mode (no DBus for demo)
    daemon = Daemon(
        str(config_dir),
        enable_dbus=False,
        log_events=True,
        log_dir=config_dir / "logs",  # keep demo telemetry out of the real log dir
        state_dir=config_dir / "state",
    )
    daemon.start()

    # Create overlay in fallback mode (never auto-hide during the demo)
    app = QApplication.instance() or QApplication([])
    overlay = OverlayWindow(dbus_available=False, timeout_seconds=0)
    overlay.show()

    print("\nDemonstration: Simulating desktop events...")
    print("-" * 40)

    # Simulate some events to show the pipeline in action
    events = [
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "desktop_state",
            "action": "show_desktop",
            "metadata": {"window": "unknown", "desktop": 1},
        },
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "window_state",
            "action": "tile_left",
            "metadata": {"window": "Terminal", "maximized": False},
        },
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "window_focus",
            "action": "window_focus",
            "metadata": {"window": "Browser", "app": "firefox"},
        },
    ]

    # Send events and update overlay
    for i, event in enumerate(events):
        print(f"\nEvent {i + 1}: {event['action']}")
        event_json = json.dumps(event)

        # Define callback to update overlay with suggestions
        def create_callback() -> Callable[[list[Any]], None]:
            def callback(suggestions: list[Any]) -> None:
                # Update overlay with suggestions
                overlay.set_suggestions_fallback(
                    [
                        {
                            "action": s.action,
                            "key": s.key,
                            "description": s.description,
                            "priority": s.priority,
                        }
                        for s in suggestions
                    ]
                )

            return callback

        daemon.set_suggestions_callback(create_callback())
        daemon.send_event(event_json)

        # Pause between events while keeping the UI responsive
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)

    print("\nDemo completed! Suggestions should be visible on overlay.")

    # Keep the application running
    print("Keep this window open to see the overlay. Press Ctrl+C to exit.")
    import signal

    from PySide6.QtCore import QTimer

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    heartbeat = QTimer()  # lets Python run the signal handler while Qt is idle
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(250)
    app.exec()
    print("\nShutting down...")

    daemon.stop()


if __name__ == "__main__":
    run_demo()
