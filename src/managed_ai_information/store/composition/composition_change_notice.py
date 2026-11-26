"""Compare a Git worktree with a pinned commit and consult a saved composition."""

import contextlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import CHANGE_NOTICE_FORMAT
from managed_ai_information.store.composition.composition import (
    MAX_VIEW_BYTES,
    Composition,
    _integer,
)

GIT_BYTES = 4 * 1024 * 1024
GIT_SECONDS = 15


def git(repo: Path | str, *args: str) -> bytes:
    """Run a bounded read-only Git command cleanly on both POSIX and Windows."""
    git_exe = shutil.which("git")
    if not git_exe:
        raise ValueError(f"Git {args[0]} failed. Check the repository and base revision.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    child = subprocess.Popen(  # noqa: S603
        [git_exe, "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    data = bytearray()
    overflow = threading.Event()
    read_error: list[BaseException] = []

    def reader() -> None:
        stream = child.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                if len(data) + len(chunk) > GIT_BYTES:
                    overflow.set()
                    with contextlib.suppress(OSError):
                        child.kill()
                    break
                data.extend(chunk)
        except BaseException as exc:  # pragma: no cover - defensive pipe read guard
            read_error.append(exc)
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    deadline = time.monotonic() + GIT_SECONDS

    try:
        remaining = max(0.0, deadline - time.monotonic())
        worker.join(timeout=remaining)

        if worker.is_alive():
            child.kill()
            worker.join(timeout=2.0)
            raise ValueError(f"Git {args[0]} exceeded the time bound")

        if overflow.is_set():
            child.wait(timeout=2.0)
            raise ValueError(f"Git {args[0]} exceeded the 4 MiB input bound")

        if read_error:
            raise ValueError(
                f"Git {args[0]} failed. Check the repository and base revision."
            ) from read_error[0]

        try:
            returncode = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Git {args[0]} exceeded the time bound") from exc

        if returncode:
            raise ValueError(f"Git {args[0]} failed. Check the repository and base revision.")

        return bytes(data)
    finally:
        if child.poll() is None:
            child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=2.0)

        if child.stdout is not None and not child.stdout.closed:
            with contextlib.suppress(OSError):
                child.stdout.close()


def notice(
    system: Any,
    ident: str,
    repo: Path | str,
    base: str,
    limit: int,
) -> dict[str, Any]:
    _integer(limit, "notice limit", 200)
    record = system.show(ident)
    body, rows, _, _ = Composition(system).profile(record)

    owners: dict[str, set[str]] = {}
    for key, node in rows["elements"].items():
        detail = node.get("content")
        paths = detail.get("paths", []) if isinstance(detail, dict) else []
        if not isinstance(paths, list) or any(
            not isinstance(p, str)
            or "\0" in p
            or any(part in ("", ".", "..") for part in p.split("/"))
            for p in paths
        ):
            raise ValueError("element paths must be exact Git-root-relative file names")
        owners[key] = set(paths)

    top_raw = git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n").removesuffix(b"\r")
    root = Path(os.fsdecode(top_raw))
    commit = (
        git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    )

    raw = git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--ignore-submodules=none",
        "--name-only",
        "-z",
        commit,
        "--",
    )
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    changed = {os.fsdecode(p) for data in (raw, untracked) for p in data.split(b"\0") if p}

    touched = {i for i, names in owners.items() if names & changed}
    review: set[str] = set().union(*(owners[i] & changed for i in touched))
    candidates: list[dict[str, Any]] = []

    for seam in rows["relations"].values():
        ends = set(seam.get("from", []) + seam.get("to", []))
        if ends - owners.keys():
            raise ValueError("unresolved endpoint in relation " + seam["id"])
        if ends & touched:
            candidates.append({"id": seam["id"], "prediction": seam["prediction"]})
            review.update(*(owners[i] for i in ends))

    unmapped = changed - set().union(*owners.values())
    counts: dict[str, int] = {}
    truncated: list[str] = []
    report: dict[str, Any] = {
        "format": CHANGE_NOTICE_FORMAT,
        "record": {k: record[k] for k in ("id", "revision", "sha256")},
        "base": commit,
        "comparison": "changes-present" if changed else "no-visible-changes",
        "assessment": "unassessed",
        "coverage": body.get("coverage"),
        "unknowns": body.get("unknowns", []),
        "scope": (
            "Exact nominated paths and one incident relation hop. Ignored files, "
            "external recipients, undeclared dependencies and nested submodule paths "
            "are outside this view. "
            "Changes nominate a receiving check. Predictions require observation."
        ),
        "counts": counts,
        "truncated": truncated,
    }

    sections: list[tuple[str, list[Any]]] = [
        ("changed", sorted(changed)),
        ("touched_elements", sorted(touched)),
        ("candidate_relations", candidates),
        ("suggested_sources", sorted(review)),
        ("unmapped_changes", sorted(unmapped)),
    ]
    for name, values in sections:
        counts[name] = len(values)
        report[name] = values[:limit]
        if len(values) > limit:
            truncated.append(name)

    if len(json.dumps(report, indent=2)) > MAX_VIEW_BYTES:
        raise ValueError(
            "change notice exceeds the 1 MiB output bound; reduce detail or profile size"
        )
    return report
