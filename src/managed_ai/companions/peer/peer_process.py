"""Platform-abstracted worker subprocess execution, pipe pumping, and process-group cleanup."""

import os
import subprocess
import threading
import time
from typing import Any

from managed_ai.platform import (
    child_cpu_times,
    env_policy_description,
    get_platform,
    isolated_process_group_kwargs,
    process_group_exists,
    signal_process_group,
    worker_environment,
)

PLATFORM = get_platform()


def pipe_reader(item: dict[str, Any], channel: str, stream: Any, events: Any) -> None:
    """Drain one child pipe into the harness event queue until EOF."""
    fd = stream.fileno()
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            events.put((item, channel, chunk))
    finally:
        events.put((item, channel, None))


def group_exists(item: dict[str, Any]) -> bool:
    """Return True if the worker process or its isolated process group is still alive."""
    process = item.get("process")
    if process is None:
        return False
    return process_group_exists(process)


def signal_group(item: dict[str, Any], kind: str) -> None:
    """Signal a worker process group ('term' or 'kill') while avoiding post-exit PID reuse."""
    process = item.get("process")
    if process is None:
        return
    if kind not in ("term", "kill"):
        raise ValueError(f"unsupported process group signal kind: {kind!r}")

    if (
        kind == "term"
        and process.poll() is not None
        and not PLATFORM.should_signal_group_on_normal_exit
    ):
        return
    if (
        kind == "kill"
        and process.poll() is not None
        and not item["streams"]
        and not PLATFORM.should_signal_group_on_normal_exit
    ):
        return

    if signal_process_group(process, kind):
        item["cleanup"]["term_sent" if kind == "term" else "kill_sent"] = True


def spawn_worker_process(
    command: list[str],
    request_path: Any,
    workspace: Any,
    extra_env: dict[str, str],
    item: dict[str, Any],
    pipe_events: Any,
) -> subprocess.Popen[Any]:
    """Launch a worker in an isolated process group and start stdout/stderr reader threads."""
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": workspace,
        "env": worker_environment(extra_env),
        **isolated_process_group_kwargs(),
    }

    with request_path.open("rb") as incoming:
        process = subprocess.Popen(command, stdin=incoming, **popen_kwargs)  # noqa: S603

    item["process"] = process
    for channel in ("stdout", "stderr"):
        stream = getattr(process, channel)
        item["streams"][channel] = stream
        reader = threading.Thread(
            target=pipe_reader,
            args=(item, channel, stream, pipe_events),
            daemon=True,
        )
        reader.start()
        item["threads"].append(reader)

    return process


def finish_worker_process(harness: Any, item: dict[str, Any], reason: str) -> None:
    """Terminate, reap, drain, and record a worker subprocess cleanly across platforms."""
    process = item["process"]
    if (
        PLATFORM.should_signal_group_on_normal_exit
        or reason != "completed"
        or process.poll() is None
    ):
        signal_group(item, "term")

    grace = harness.limits["terminate_grace_seconds"]
    until = time.monotonic() + grace
    while time.monotonic() < until:
        harness.pump(0)
        process.poll()
        if not group_exists(item) and not item["streams"]:
            break
        harness.pump(min(0.01, max(0.0, until - time.monotonic())))

    if group_exists(item) or (PLATFORM.force_kill_if_streams_open and item["streams"]):
        signal_group(item, "kill")

    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        harness.fail("harness_error", "Local process did not reap after forced termination")

    drain_until = time.monotonic() + min(0.15, grace)
    while item["streams"] and time.monotonic() < drain_until:
        harness.pump(0.005)

    if item["streams"]:
        item["cleanup"]["streams_closed_by_host"] = True
        item["truncated"] = True
        for channel in list(item["streams"]):
            harness.close_channel(item, channel)

    for reader in item["threads"]:
        reader.join(timeout=0.05)

    if reason == "completed" and item["truncated"]:
        reason = "output_limit"
        harness.fail("output_limit", "Worker output was truncated during process cleanup")

    harness.record_process(item, reason)


__all__ = [
    "PLATFORM",
    "child_cpu_times",
    "env_policy_description",
    "finish_worker_process",
    "group_exists",
    "pipe_reader",
    "signal_group",
    "spawn_worker_process",
    "worker_environment",
]
