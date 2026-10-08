"""Shared helpers: progress reporting, cancellation, formatting, logging."""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from pathlib import Path

APP_NAME = "SectorSmith"
APP_VERSION = "1.3.0"


class Cancelled(Exception):
    """Raised inside an engine when the user presses Cancel. ``info`` says what was done before it stopped."""

    def __init__(self, msg: str = "Cancelled", **info):
        super().__init__(msg)
        self.info = info


_scope = threading.local()


@contextlib.contextmanager
def cancel_scope(check):
    """Long calls made by this thread inside the block (endpoint ops, subprocesses, VSS, used-space maps, Link
    calls to another PC) call ``check()`` while they wait, so Cancel stops them too. ``check`` raises Cancelled."""
    prev = getattr(_scope, "check", None)
    _scope.check = check
    try:
        yield
    finally:
        _scope.check = prev


def current_check():
    return getattr(_scope, "check", None)


def check_cancel():
    """Raise Cancelled if the job this thread works for was cancelled (no-op outside a cancel_scope)."""
    c = getattr(_scope, "check", None)
    if c is not None:
        c()


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_logger() -> logging.Logger:
    log = logging.getLogger(APP_NAME)
    if not log.handlers:
        log.setLevel(logging.INFO)
        try:
            fh = logging.FileHandler(app_dir() / "sectorsmith.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(fh)
        except OSError:
            pass
    return log


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_time(sec: float | None) -> str:
    if sec is None or sec != sec or sec == float("inf"):
        return "--:--:--"
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class Progress:
    """Thread-safe progress tracker with moving-average speed and ETA.

    Engines call ``update(done_bytes)`` and ``check()``; the GUI reads ``snapshot()``.
    """

    def __init__(self, total: int, label: str = "", callback=None):
        self.total = max(1, int(total))
        self.done = 0
        self.label = label
        self.detail = ""
        self.start = time.monotonic()
        self._cancel = threading.Event()
        self._samples: list[tuple[float, int]] = [(self.start, 0)]
        self._lock = threading.Lock()
        self.callback = callback
        self._last_cb = 0.0

    # --- control -------------------------------------------------------
    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # --- reporting -----------------------------------------------------
    def set_label(self, label: str, detail: str | None = None):
        with self._lock:
            self.label = label
            if detail is not None:
                self.detail = detail
        self._maybe_cb(force=True)

    def set_detail(self, detail: str):
        with self._lock:
            self.detail = detail
        self._maybe_cb()

    def update(self, done: int):
        now = time.monotonic()
        with self._lock:
            self.done = min(int(done), self.total)
            self._samples.append((now, self.done))
            # keep a ~3 second window for speed
            while len(self._samples) > 2 and now - self._samples[0][0] > 3.0:
                self._samples.pop(0)
        self._maybe_cb()

    def reset(self, total: int, label: str | None = None):
        with self._lock:
            self.total = max(1, int(total))
            self.done = 0
            self.start = time.monotonic()
            self._samples = [(self.start, 0)]
            if label is not None:
                self.label = label
        self._maybe_cb(force=True)

    def speed(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        return (b1 - b0) / dt if dt > 0 else 0.0

    def snapshot(self) -> dict:
        spd = self.speed()
        remaining = self.total - self.done
        eta = remaining / spd if spd > 0 else None
        return {
            "label": self.label,
            "detail": self.detail,
            "done": self.done,
            "total": self.total,
            "pct": 100.0 * self.done / self.total,
            "speed": spd,
            "eta": eta,
            "elapsed": time.monotonic() - self.start,
        }

    def _maybe_cb(self, force=False):
        if self.callback is None:
            return
        now = time.monotonic()
        if force or now - self._last_cb > 0.25:
            self._last_cb = now
            try:
                self.callback(self.snapshot())
            except Exception:  # never let a UI callback kill an engine
                pass


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if is_windows():
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def relaunch_as_admin() -> bool:
    """On Windows, restart this program elevated (UAC prompt). Returns True if a new process started."""
    if not is_windows() or is_admin():
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe") if sys.executable.endswith("python.exe") \
            else sys.executable
        params = "-m sectorsmith " + " ".join(f'"{a}"' for a in sys.argv[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, os.getcwd(), 1)
    return rc > 32
