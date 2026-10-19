"""Optional, process-local progress reporting; never touches indexed data."""

from collections.abc import Callable
import logging
import math
from threading import Event, Lock, Thread, TIMEOUT_MAX
from time import monotonic


class ProgressReporter:
    """No-op default for library callers. All counters describe attempted work."""

    enabled = False

    def start_step(self, number: int, total: int, name: str) -> None:
        pass

    def phase(self, name: str, *, total: int | None = None, unit: str = "", detail: str = "") -> None:
        pass

    def update(self, completed: int | None = None, *, detail: str | None = None) -> None:
        pass

    def finish_step(self) -> None:
        pass

    def fail_step(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


NO_PROGRESS = ProgressReporter()


class LoggingProgressReporter(ProgressReporter):
    """One owner thread updates state; a heartbeat thread only formats that state.

    Constructing the reporter starts no thread. Use it as a context manager to
    enable heartbeats. tick() also supports deterministic, clock-driven tests.
    """

    enabled = True

    def __init__(self, interval: float = 5, *, clock: Callable[[], float] = monotonic,
                 emit: Callable[[str], None] | None = None):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("progress interval must be finite and positive")
        self.interval = interval
        self._clock = clock
        self._emit = emit if emit is not None else logging.getLogger(__name__).info
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._step = None
        self._phase = None

    def __enter__(self):
        if self._thread is not None:
            raise RuntimeError("progress reporter is already running")
        self._stop.clear()
        self._thread = Thread(target=self._heartbeat, name="index-progress", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self._step = self._phase = None

    def _heartbeat(self):
        delay = self.interval
        while not self._stop.wait(min(delay, TIMEOUT_MAX)):
            self.tick()
            with self._lock:
                delay = (max(0.001, self.interval - (self._clock() - self._last_emit))
                         if self._step is not None and self._phase is not None else self.interval)

    def start_step(self, number, total, name):
        with self._lock:
            self._step = f"[{number}/{total}] {name}"
            self._label = f"step {number}/{total}: {name}"
            self._step_start = self._clock()
            self._phase = None
            self._emit(f"Starting step {number}/{total}: {name}")

    def phase(self, name, *, total=None, unit="", detail=""):
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("progress total must be a nonnegative integer")
        with self._lock:
            if self._step is None:
                return
            self._phase = name
            self._total, self._unit, self._detail = total, unit, detail
            self._completed = self._samples = 0
            self._phase_start = self._clock()
            self._deadline = None
            self._write(self._phase_start)

    def update(self, completed=None, *, detail=None):
        with self._lock:
            if self._step is None or self._phase is None:
                return
            now = self._clock()
            changed = completed is not None and completed != self._completed
            if completed is not None:
                if (type(completed) is not int or completed < self._completed
                        or (self._total is not None and completed > self._total)):
                    raise ValueError("progress must be monotonic and within its total")
                if changed:
                    self._samples += 1
                    self._completed = completed
                    elapsed = now - self._phase_start
                    if self._total is not None and self._samples >= 3 and elapsed >= 5:
                        self._deadline = now + (self._total - completed) * elapsed / completed
            if detail is not None:
                self._detail = detail
            if changed and self._completed == self._total:
                self._write(now)

    def tick(self):
        with self._lock:
            if (self._step is not None and self._phase is not None
                    and self._clock() - self._last_emit >= self.interval):
                self._write(self._clock())

    def _write(self, now):
        parts = [self._step, self._phase]
        if self._total is not None:
            if self._unit == "bytes":
                if self._total < 1048576:
                    parts.append(f"{self._completed}/{self._total} bytes")
                else:
                    parts.append(f"{self._completed / 1048576:.1f}/{self._total / 1048576:.1f} MiB")
            else:
                parts.append(f"{self._completed}/{self._total} {self._unit}".rstrip())
        parts.append(f"elapsed {now - self._phase_start:.0f}s")
        eta = "unknown"
        if self._total is not None and self._completed == self._total:
            eta = "0s"
        elif self._deadline is not None and self._deadline > now:
            eta = f"~{math.ceil(self._deadline - now)}s"
        parts.append(f"remaining {eta}")
        if self._detail:
            parts.append(" ".join(self._detail.splitlines()))
        self._emit(" | ".join(parts))
        self._last_emit = now

    def _end(self, outcome):
        with self._lock:
            if self._step is not None:
                self._emit(f"{outcome} {self._label} | total elapsed {self._clock() - self._step_start:.1f}s")
                self._step = self._phase = None

    def finish_step(self):
        self._end("Completed")

    def fail_step(self):
        self._end("Failed or interrupted")
