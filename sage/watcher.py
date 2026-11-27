"""Configuration file watcher for hot-reload."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class ConfigWatcher:
    """Watches config files for changes and triggers callbacks."""

    def __init__(
        self,
        config_dir: Path | str,
        callback: Callable[[str], object],
        debounce_seconds: float = 0.2,
    ):
        """
        Initialize config watcher.

        Args:
            config_dir: Directory containing config files to watch
            callback: Function to call when a config file changes (receives filename)
            debounce_seconds: Collapse bursts of events for the same file
        """
        self.config_dir = Path(config_dir)
        self.callback = callback
        self.observer: Any = None
        self._handler = _ConfigHandler(self.config_dir, self.callback, debounce_seconds)

    def start(self) -> None:
        """Start watching for file changes."""
        if self.observer is not None:
            logger.warning("Watcher already started")
            return

        self.observer = Observer()
        self.observer.schedule(self._handler, str(self.config_dir), recursive=False)
        self.observer.start()
        logger.info(f"Started watching config directory: {self.config_dir}")

    def stop(self) -> None:
        """Stop watching for file changes."""
        if self.observer is None:
            logger.debug("Watcher not started")
            return

        self.observer.stop()
        self.observer.join(timeout=5.0)
        self.observer = None
        self._handler.cancel_pending()
        logger.info("Stopped watching config directory")

    def __enter__(self) -> ConfigWatcher:
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        """Context manager exit."""
        self.stop()


def _as_str(path: Any) -> str:
    return path if isinstance(path, str) else bytes(path).decode("utf-8")


class _ConfigHandler(FileSystemEventHandler):
    """Internal handler for config file system events.

    Handles in-place writes (``modified``), new files (``created``) and atomic
    save-by-rename (``moved``), which is what most editors (vim, VS Code, Kate) do.
    """

    def __init__(self, config_dir: Path, callback: Callable[[str], object], debounce: float):
        super().__init__()
        self.config_dir = config_dir
        self.callback = callback
        self.debounce = debounce
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _dispatch_path(self, raw_path: Any) -> None:
        path = _as_str(raw_path)
        if not path.endswith((".yaml", ".yml")):
            return
        if Path(path).parent.resolve() != self.config_dir.resolve():
            return

        filename = Path(path).name
        if self.debounce <= 0:
            self._fire(filename)
            return

        # Trailing-edge debounce: a save often produces several events (truncate,
        # write, rename). Fire once, after the burst, so we never read a half-written
        # file.
        with self._lock:
            existing = self._timers.get(filename)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self.debounce, self._fire, args=(filename,))
            timer.daemon = True
            self._timers[filename] = timer
            timer.start()

    def _fire(self, filename: str) -> None:
        with self._lock:
            self._timers.pop(filename, None)
        logger.debug(f"Config file changed: {filename}")
        try:
            self.callback(filename)
        except Exception as e:
            logger.error(f"Error in config reload callback for {filename}: {e}")

    def cancel_pending(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._dispatch_path(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._dispatch_path(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._dispatch_path(getattr(event, "dest_path", event.src_path))
