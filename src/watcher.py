"""
src/watcher.py
─────────────────────────────────────────────────────────────────────────────
Monitors input_instagram/ and input_behance/ for newly dropped project folders.

Reliability strategy (three layers):
  1. PollingObserver  — polls every 2s; works on all OS without special
                        permissions. Slightly more CPU than FSEvents but
                        100% reliable on macOS / network drives / Docker.
  2. Periodic scanner — a separate thread that scans the input dirs every
                        10s and enqueues any folder not yet seen. Catches
                        folders that were dropped before launch, or that the
                        OS-level event missed.
  3. UI "Scan Now"    — calls scan_once() on demand from the browser.
"""

import time
import threading
from pathlib import Path
from typing import Callable

from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from src.file_queue import push as fq_push

BASE_DIR        = Path(__file__).resolve().parent.parent
INPUT_DIRS: dict[str, Path] = {
    "instagram": BASE_DIR / "input_instagram",
    "behance":   BASE_DIR / "input_behance",
}

VALID_EXTS     = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
POLL_INTERVAL  = 2    # seconds — PollingObserver tick
SCAN_INTERVAL  = 10   # seconds — background directory scan

DetectedCallback = Callable[[str, Path, int], None]


# ── Shared seen-set (across both layers) ──────────────────────────────────────
_seen: set[Path] = set()
_seen_lock       = threading.Lock()


def _try_enqueue(platform: str, path: Path, on_detected: DetectedCallback | None) -> bool:
    """
    Validate and enqueue *path* for *platform*.
    Returns True if a new project was queued, False if skipped.
    Thread-safe.
    """
    path = path.resolve()

    with _seen_lock:
        if path in _seen:
            return False
        # Mark immediately so concurrent events don't double-enqueue
        _seen.add(path)

    # Wait briefly for OS to finish copying files into the folder
    time.sleep(1.5)

    if not path.is_dir():
        return False

    try:
        images = [f for f in path.iterdir() if f.suffix.lower() in VALID_EXTS]
    except OSError:
        return False

    if not images:
        print(f"  [watcher:{platform}] Skipped (no images): {path.name}")
        # Remove from seen so user can retry after adding images
        with _seen_lock:
            _seen.discard(path)
        return False

    fq_push(platform, path)
    print(f"  [watcher:{platform}] Queued: {path.name} ({len(images)} image(s))")

    if on_detected:
        try:
            on_detected(platform, path, len(images))
        except Exception:
            pass

    return True


# ── Layer 1: PollingObserver event handler ────────────────────────────────────
class _Handler(FileSystemEventHandler):
    def __init__(self, platform: str, on_detected: DetectedCallback | None) -> None:
        super().__init__()
        self._platform    = platform
        self._on_detected = on_detected

    def on_any_event(self, event: FileSystemEvent) -> None:
        # React to any event inside the watched dir — dir created/moved OR
        # a file created/modified (handles slow copy-in where dir event fires first)
        raw = getattr(event, "dest_path", None) or event.src_path
        candidate = Path(raw)

        # If the event is on a file, enqueue its parent directory
        if not event.is_directory:
            candidate = candidate.parent

        # Only care about direct children of the input folder (not sub-subdirs)
        if candidate.parent != INPUT_DIRS[self._platform]:
            return

        threading.Thread(
            target=_try_enqueue,
            args=(self._platform, candidate, self._on_detected),
            daemon=True,
        ).start()


# ── Layer 2: Periodic background scanner ──────────────────────────────────────
def _scan_once(on_detected: DetectedCallback | None = None) -> int:
    """
    Scan both input dirs right now.
    Returns the number of new projects enqueued.
    """
    found = 0
    for platform, input_dir in INPUT_DIRS.items():
        if not input_dir.exists():
            continue
        for candidate in input_dir.iterdir():
            if candidate.is_dir() and not candidate.name.startswith("."):
                if _try_enqueue(platform, candidate, on_detected):
                    found += 1
    return found


def _periodic_scan(on_detected: DetectedCallback | None, stop_event: threading.Event) -> None:
    """Background thread: scan every SCAN_INTERVAL seconds."""
    while not stop_event.wait(timeout=SCAN_INTERVAL):
        _scan_once(on_detected)


# ── Public API ────────────────────────────────────────────────────────────────
def scan_once(on_detected: DetectedCallback | None = None) -> int:
    """
    Called from the UI "Scan Now" button.
    Clears the seen-set first so previously-skipped folders are retried.
    Returns count of newly queued projects.
    """
    with _seen_lock:
        _seen.clear()
    return _scan_once(on_detected)


def start_watcher(on_detected: DetectedCallback | None = None) -> PollingObserver:
    """
    Start the PollingObserver + periodic scanner thread.
    Returns the observer so the caller can stop() it on shutdown.
    """
    for d in INPUT_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

    # ── PollingObserver (Layer 1) ─────────────────────────────────────────────
    observer = PollingObserver(timeout=POLL_INTERVAL)
    for platform, input_dir in INPUT_DIRS.items():
        observer.schedule(
            _Handler(platform, on_detected),
            str(input_dir),
            recursive=False,
        )
    observer.start()

    # ── Periodic scanner (Layer 2) ────────────────────────────────────────────
    stop_event = threading.Event()
    scanner = threading.Thread(
        target=_periodic_scan,
        args=(on_detected, stop_event),
        daemon=True,
        name="dir-scanner",
    )
    scanner.start()

    # ── Initial scan on startup ───────────────────────────────────────────────
    # Catches folders that were already sitting in the input dirs before launch
    threading.Thread(
        target=lambda: (time.sleep(1), _scan_once(on_detected)),
        daemon=True,
        name="startup-scan",
    ).start()

    print(
        f"  [watcher] PollingObserver active (every {POLL_INTERVAL}s) + "
        f"scanner (every {SCAN_INTERVAL}s)"
    )
    for platform, d in INPUT_DIRS.items():
        print(f"  [watcher] Watching [{platform}]: {d}")

    return observer
