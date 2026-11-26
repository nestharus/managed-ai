"""Memory-bounded subprocess execution for signal_field worker invocations."""

import contextlib
import hashlib
import os
import subprocess
import threading
import time
from typing import Any

from .config import (
    MAX_WORKER_DRAIN_BYTES,
    MAX_WORKER_STDOUT_BYTES,
    MAX_WORKER_STREAM_BYTES,
)
from .models import StreamCapture, WorkerRunResult


def drain_stream(
    stream: Any,
    capture: StreamCapture,
    proc: subprocess.Popen[bytes],
    drain_limit: int,
) -> None:
    """Drain one worker pipe incrementally while tracking exact byte count and SHA-256."""
    hasher = hashlib.sha256()
    try:
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break

            hasher.update(chunk)
            capture.total_bytes += len(chunk)

            room = capture.keep_limit - len(capture.kept)
            if room > 0:
                capture.kept.extend(chunk[:room])
            if capture.total_bytes > capture.keep_limit:
                capture.limit_exceeded = True

            if capture.total_bytes > drain_limit:
                capture.overflowed = True
                with contextlib.suppress(OSError):
                    proc.kill()
                break
    finally:
        capture.sha256 = hasher.hexdigest()
        with contextlib.suppress(OSError):
            stream.close()


def feed_stdin(stream: Any, payload: bytes) -> None:
    """Write request bytes to worker stdin in bounded chunks and close EOF."""
    try:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = stream.write(view[offset : offset + 65536])
            if not written:
                break
            offset += written
        stream.flush()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def run_worker_bounded(
    argv: list[str],
    payload: bytes,
    timeout: int | float,
    *,
    max_stdout_bytes: int = MAX_WORKER_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_WORKER_STREAM_BYTES,
    max_drain_bytes: int = MAX_WORKER_DRAIN_BYTES,
) -> WorkerRunResult:
    """Invoke worker subprocess without shell and bound stdout/stderr memory usage."""
    stdout_cap = StreamCapture(max_stdout_bytes)
    stderr_cap = StreamCapture(max_stderr_bytes)

    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    feeder = threading.Thread(target=feed_stdin, args=(proc.stdin, payload), daemon=True)
    out_reader = threading.Thread(
        target=drain_stream,
        args=(proc.stdout, stdout_cap, proc, max_drain_bytes),
        daemon=True,
    )
    err_reader = threading.Thread(
        target=drain_stream,
        args=(proc.stderr, stderr_cap, proc, max_drain_bytes),
        daemon=True,
    )

    feeder.start()
    out_reader.start()
    err_reader.start()

    timed_out = False
    timeout_error: str | None = None

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        timeout_error = str(exc)
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5.0)

    join_wait = 0.5 if timed_out else max(1.0, min(float(timeout), 5.0))
    deadline = time.monotonic() + join_wait
    for reader in (out_reader, err_reader):
        remaining = max(0.0, deadline - time.monotonic())
        reader.join(timeout=remaining)
    feeder.join(timeout=0.5)

    return WorkerRunResult(
        returncode=None if timed_out else proc.returncode,
        stdout=stdout_cap,
        stderr=stderr_cap,
        timed_out=timed_out,
        error=timeout_error,
    )
