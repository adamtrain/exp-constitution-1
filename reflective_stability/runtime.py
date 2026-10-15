"""Running a long, unattended experiment: progress output, parallel workers, stopping, safe storage.

Ctrl-C once: start no new calls; calls already in flight finish and are saved, then the run exits.
Ctrl-C again (2+ seconds later): kill the in-flight `claude` processes and exit at once.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

STOP = threading.Event()
_print_lock = threading.Lock()
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


class Stopped(Exception):
    """Raised inside a worker once the run is stopping; the current unit of work is abandoned unsaved."""


def say(message: str) -> None:
    with _print_lock:
        print(f"{datetime.now():%H:%M:%S}  {message}", flush=True)


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Progress:
    """Counts finished units of one stage and prefixes every line with where the stage stands."""

    def __init__(self, stage: str, total: int, already_done: int, spent: float = 0.0):
        self.stage, self.total, self.done, self.spent = stage, total, already_done, spent
        self.started, self.done_here = time.time(), 0
        self._lock = threading.Lock()

    def advance(self, message: str, cost: float = 0.0) -> None:
        with self._lock:
            self.done += 1
            self.done_here += 1
            self.spent += cost
            elapsed = time.time() - self.started
            eta = elapsed / self.done_here * (self.total - self.done)
            prefix = (
                f"[{self.stage} {self.done}/{self.total} {self.done / max(1, self.total):4.0%}"
                f" | ${self.spent:,.2f} | {duration(elapsed)} elapsed, ~{duration(eta)} left]"
            )
        say(f"{prefix} {message}")


def run_parallel(jobs: Iterable, work: Callable, parallel: int) -> None:
    """Run work(job) for every job on `parallel` threads.

    Ctrl-C or the first error sets STOP: workers start nothing new, calls in flight finish and are
    saved, then the error (or KeyboardInterrupt) is raised. A second Ctrl-C kills calls in flight.
    """
    first_error = None
    previous_handler = signal.signal(signal.SIGINT, _on_interrupt)
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for future in as_completed([pool.submit(work, job) for job in jobs]):
                try:
                    future.result()
                except Stopped:
                    pass
                except Exception as error:
                    if first_error is None:
                        first_error = error
                        STOP.set()
                        say(f"STOPPING after an error: {error}")
                        say("Waiting for calls in flight to finish and be saved (Ctrl-C twice to kill them).")
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    if first_error:
        raise first_error
    if STOP.is_set():
        raise KeyboardInterrupt


_interrupted_at: float | None = None


def _on_interrupt(signum, frame) -> None:
    global _interrupted_at
    now = time.time()
    if _interrupted_at is None:
        _interrupted_at = now
        STOP.set()
        say("INTERRUPTED. Finishing the calls already in flight so nothing is lost; Ctrl-C again to kill them.")
    elif now - _interrupted_at > 2:  # one keypress can arrive twice (the terminal and `uv run` both send it)
        kill_children()
        say("Killed the calls in flight. Rerun the same command to resume.")
        os._exit(130)


def run_child(command: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
    """Run a subprocess in its own session (so Ctrl-C reaches only us) and track it for kill_children."""
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_neutral_dir,
        start_new_session=True,
    )
    with _children_lock:
        _children.add(child)
    try:
        stdout, stderr = child.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise
    finally:
        with _children_lock:
            _children.discard(child)
    return child.returncode, stdout, stderr


def kill_children() -> None:
    with _children_lock:
        for child in _children:
            child.kill()


# Calls run from an empty directory so the working directory Claude Code reports reveals nothing.
_neutral_dir = tempfile.mkdtemp(prefix="session-")


def read_jsonl(path: Path) -> list[dict]:
    """Read one JSON object per line. An unterminated last line (from a crash mid-write) is removed."""
    if not path.exists():
        return []
    text = path.read_text()
    if text and not text.endswith("\n"):
        keep = text.rfind("\n") + 1
        say(f"warning: removing an incomplete last line from {path}")
        with path.open("r+") as f:
            f.truncate(len(text[:keep].encode()))
        text = text[:keep]
    return [json.loads(line) for line in text.split("\n") if line.strip()]


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def exit_with(message: str) -> None:
    say(message)
    sys.exit(1)
