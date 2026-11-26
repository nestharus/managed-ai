"""Portable consumer of declared observations and the exact accepted assessor.

The source owner supplies dependency meaning. This module nominates review; it
never edits publications, discovers dependencies, or releases independent duties.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any
import uuid

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.companions.publication_review import (
    continuation_assessor as _default_assessor,
    publication_observer as _default_observer,
)
from managed_ai.platform import (
    check_unfollowed_ancestors as _check_unfollowed_ancestors,
    is_reparse_or_symlink as _is_reparse_or_symlink,
    strict_json_loads,
    sync_dir as _sync_dir,
)

HERE = Path(__file__).resolve().parent


def _file_identity(path: Path | str) -> dict[str, Any]:
    body = Path(path).read_bytes()
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def _check_unfollowed_ordinary_file(raw_path: Path | str) -> Path:
    abs_path = _check_unfollowed_ancestors(raw_path, "Explicit assessor", include_target=True)
    try:
        info = abs_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Explicit assessor must be an existing ordinary file: " + str(abs_path)) from exc

    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Explicit assessor must be an existing ordinary file: " + str(abs_path))
    return abs_path.resolve()


def _load_assessor(path: Path | str | None = None) -> tuple[Any, dict[str, Any]]:
    """Return the colocated assessor module or load an explicit non-symlink override."""
    if path is None:
        assessor_file = Path(_default_assessor.__file__).resolve()
        return _default_assessor, _file_identity(assessor_file)

    resolved = _check_unfollowed_ordinary_file(path)
    spec = importlib.util.spec_from_file_location("_g60_publication_assessor", resolved)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load explicit assessor module: " + str(resolved))

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "assess", None)):
        raise ValueError("Explicit assessor module must define callable assess(previous, current)")
    return module, _file_identity(resolved)


def assess_documents(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    assessor_path: Path | str | None = None,
) -> dict[str, Any]:
    """Invoke the assessor directly and return its exact result without mutating inputs."""
    assessor, _ = _load_assessor(assessor_path)
    return assessor.assess(previous, current)


def review(
    previous: dict[str, Any],
    declaration: dict[str, Any],
    roots: dict[str, Any] | None = None,
    *,
    base_dir: Path | str | None = None,
    assessor_path: Path | str | None = None,
) -> dict[str, Any]:
    """Observe a declaration and assess it against the unmodified previous basis."""
    observation = _default_observer.observe(declaration, roots=roots, base_dir=base_dir)
    return review_observations(previous, observation, assessor_path=assessor_path)


def review_files(
    previous_path: Path | str,
    declaration_path: Path | str,
    roots: dict[str, Any] | None = None,
    *,
    assessor_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read explicit JSON inputs; declaration-relative paths use its directory."""
    previous = _read_json(previous_path)
    observation = _default_observer.observe_file(declaration_path, roots=roots)
    return review_observations(previous, observation, assessor_path=assessor_path)


def explain(assessment: dict[str, Any]) -> list[dict[str, Any]]:
    """Render every result and reason without changing the structured result."""
    meanings = {
        "current": "All declared comparisons match the supplied observations.",
        "recheck": (
            "A declared input or output changed or was observed absent; "
            "the owner should reconsider this artifact."
        ),
        "unknown": "Necessary observations or a discovery selection basis are unavailable.",
    }
    return [
        {
            "id": artifact["id"],
            "status": artifact["status"],
            "explanation": meanings[artifact["status"]],
            "reasons": copy.deepcopy(artifact["reasons"]),
        }
        for artifact in assessment["artifacts"]
    ]


def review_observations(
    previous: dict[str, Any],
    observation: dict[str, Any],
    *,
    assessor_path: Path | str | None = None,
) -> dict[str, Any]:
    """Consume {current, origins}, retaining both alongside the exact assessment."""
    if not isinstance(observation, dict) or not isinstance(observation.get("current"), dict):
        raise ValueError("Observation must contain a current object")
    if not isinstance(observation.get("origins"), dict):
        raise ValueError("Observation must contain an origins object")

    assessor, assessor_identity = _load_assessor(assessor_path)
    result = assessor.assess(previous, observation["current"])
    return {
        "schema": "publication-review/1",
        "observation": copy.deepcopy(observation),
        "assessment": result,
        "current": copy.deepcopy(observation["current"]),
        "origins": copy.deepcopy(observation["origins"]),
        "explanations": explain(result),
        "assessor": assessor_identity,
        "scope": (
            "Declared-input currentness using supplied observations. Recheck nominates owner work; "
            "it does not establish falsehood, authorize edits, or close obligations."
        ),
    }


def _read_json(path: Path | str) -> Any:
    return strict_json_loads(
        Path(path).read_bytes(),
        label=str(path),
        require_object=False,
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _retain(output_dir: Path | str, previous: dict[str, Any], report: dict[str, Any]) -> str:
    """Atomically publish one fresh occurrence directory without overwriting or leaving partial files."""
    target = _check_unfollowed_ancestors(output_dir, "Output directory", include_target=True)
    if target.exists() or _is_reparse_or_symlink(target):
        raise FileExistsError("Output directory already exists: " + str(target))
    if not target.parent.is_dir() or _is_reparse_or_symlink(target.parent):
        raise FileNotFoundError("Output parent directory does not exist: " + str(target.parent))

    staging = target.parent / (".retain-" + target.name + "-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        values = {
            "previous.json": previous,
            "current.json": report["current"],
            "observed-origins.json": report["origins"],
            "observation.json": report["observation"],
            "assessment.json": report["assessment"],
            "report.json": report,
        }
        for name, value in values.items():
            payload = _json_text(value).encode("utf-8")
            with (staging / name).open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        _sync_dir(staging)
        staging.rename(target)
        _sync_dir(target.parent)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return str(target.resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--previous", required=True, help="Previous assessor receipts JSON")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--declaration", help="Declared input bindings JSON; observe then assess")
    inputs.add_argument("--current", help="Current assessor observations JSON")
    inputs.add_argument("--observation", help="Producer JSON object containing current and origins")
    parser.add_argument("--origins", help="Origins JSON accompanying --current")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Override a named declaration root; repeat for different names",
    )
    parser.add_argument("--assessor", help="Optional explicit path to an assessor module")
    parser.add_argument("--output-dir", help="Fresh requested directory; otherwise full report goes to stdout")
    args = parser.parse_args(argv)

    try:
        previous = _read_json(args.previous)
        if args.origins and not args.current:
            raise ValueError("--origins applies only with --current")
        if args.root and not args.declaration:
            raise ValueError("--root applies only with --declaration")

        if args.declaration:
            overrides: dict[str, str] = {}
            for item in args.root:
                name, separator, location = item.partition("=")
                if not separator or not name or not location or name in overrides:
                    raise ValueError("Each --root requires a unique nonempty NAME=PATH")
                overrides[name] = os.path.abspath(location)
            observation = _default_observer.observe_file(args.declaration, roots=overrides)
        elif args.observation:
            observation = _read_json(args.observation)
        else:
            observation = {
                "current": _read_json(args.current),
                "origins": _read_json(args.origins)
                if args.origins
                else {
                    "scope": "Caller-supplied observations; origins were not provided or independently validated.",
                },
            }

        report = review_observations(previous, observation, assessor_path=args.assessor)
        if args.output_dir:
            directory = _retain(args.output_dir, previous, report)
            print(_json_text({"output_dir": directory, "assessment": report["assessment"]}), end="")
        else:
            print(_json_text(report), end="")
        return 0
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print("publication_review: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
