"""Portable consumer of declared observations and the exact accepted assessor.

The source owner supplies dependency meaning. This module nominates review; it
never edits publications, discovers dependencies, or releases independent duties.
"""
from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid

try:
    from . import continuation_assessor as _default_assessor
    from . import publication_observer as _default_observer
except ImportError:
    _src_root = Path(__file__).resolve().parents[3]
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from managed_ai.companions.publication_review import (
        continuation_assessor as _default_assessor,
        publication_observer as _default_observer,
    )


HERE = Path(__file__).resolve().parent


def _file_identity(path):
    body = Path(path).read_bytes()
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def _sync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise
    finally:
        os.close(fd)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _check_unfollowed_ancestors(raw_path, role: str, *, include_target: bool = True) -> Path:
    text = os.fspath(raw_path)
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise ValueError(role + " must be a valid nonempty path")
    raw = Path(text)
    if not raw.is_absolute():
        if raw.drive or raw.anchor:
            raise ValueError(role + " must not use a drive-relative path: " + text)
        pwd = os.environ.get("PWD")
        if pwd and os.path.isabs(pwd):
            try:
                if os.path.samefile(pwd, os.getcwd()):
                    logical = Path(pwd)
                    for ancestor in (*reversed(logical.parents), logical):
                        if _is_reparse_or_symlink(ancestor):
                            raise ValueError(role + " traverses a symlink or reparse point: " + str(ancestor))
            except OSError:
                pass
        cur = Path(os.getcwd())
        raw_parts = raw.parts
    else:
        cur = Path(raw.anchor)
        raw_parts = raw.parts[1:]
    for idx, part in enumerate(raw_parts):
        if part == ".":
            continue
        is_last = idx == len(raw_parts) - 1
        cur = cur.parent if part == ".." else cur / part
        if include_target or not is_last:
            if _is_reparse_or_symlink(cur):
                raise ValueError(role + " must not be or traverse a symlink or reparse point: " + str(cur))
    abs_path = Path(os.path.abspath(raw))
    parts = (*reversed(abs_path.parents), abs_path) if include_target else tuple(reversed(abs_path.parents))
    for part in parts:
        if _is_reparse_or_symlink(part):
            raise ValueError(role + " must not be or traverse a symlink or reparse point: " + str(part))
    return abs_path


def _check_unfollowed_ordinary_file(raw_path) -> Path:
    abs_path = _check_unfollowed_ancestors(raw_path, "Explicit assessor", include_target=True)
    try:
        info = abs_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Explicit assessor must be an existing ordinary file: " + str(abs_path)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Explicit assessor must be an existing ordinary file: " + str(abs_path))
    return abs_path.resolve()


def _load_assessor(path=None):
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


def assess_documents(previous, current, *, assessor_path=None):
    """Invoke the assessor directly and return its exact result.

    No files described by the documents are opened here. The assessor compares
    only declared identities and returns detached previous open obligations.
    Neither input document is mutated.
    """
    assessor, _ = _load_assessor(assessor_path)
    return assessor.assess(previous, current)


def review(previous, declaration, roots=None, *, base_dir=None, assessor_path=None):
    """Observe a declaration and assess it against the unmodified previous basis.

    Relative paths resolve from base_dir, defaulting to the current directory.
    roots overrides only locations, never the logical IDs or previous digests.
    The returned report retains the complete observation and exact assessment.
    """
    observation = _default_observer.observe(declaration, roots=roots, base_dir=base_dir)
    return review_observations(previous, observation, assessor_path=assessor_path)


def review_files(previous_path, declaration_path, roots=None, *, assessor_path=None):
    """Read explicit JSON inputs; declaration-relative paths use its directory."""
    previous = _read_json(previous_path)
    observation = _default_observer.observe_file(declaration_path, roots=roots)
    return review_observations(previous, observation, assessor_path=assessor_path)


def explain(assessment):
    """Render every result and reason without changing the structured result."""
    meanings = {
        "current": "All declared comparisons match the supplied observations.",
        "recheck": "A declared input or output changed or was observed absent; the owner should reconsider this artifact.",
        "unknown": "Necessary observations or a discovery selection basis are unavailable.",
    }
    rows = []
    for artifact in assessment["artifacts"]:
        rows.append({
            "id": artifact["id"],
            "status": artifact["status"],
            "explanation": meanings[artifact["status"]],
            "reasons": copy.deepcopy(artifact["reasons"]),
        })
    return rows


def review_observations(previous, observation, *, assessor_path=None):
    """Consume {current, origins}, retaining both alongside the exact assessment.

    Origins are observation evidence supplied by the producer. This consumer
    does not independently validate a caller-created observation dictionary.
    """
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
        "scope": "Declared-input currentness using supplied observations. Recheck nominates owner work; it does not establish falsehood, authorize edits, or close obligations.",
    }


def _read_json(path):
    def reject(value):
        raise ValueError("Nonfinite JSON value: " + value)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=reject,
        object_pairs_hook=pairs,
    )


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _retain(output_dir, previous, report):
    """Atomically publish one fresh occurrence directory; never overwrite or leave partial files."""
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--previous", required=True, help="Previous assessor receipts JSON")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--declaration", help="Declared input bindings JSON; observe then assess")
    inputs.add_argument("--current", help="Current assessor observations JSON")
    inputs.add_argument("--observation", help="Producer JSON object containing current and origins")
    parser.add_argument("--origins", help="Origins JSON accompanying --current")
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH",
                        help="Override a named declaration root; repeat for different names")
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
            overrides = {}
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
                "origins": _read_json(args.origins) if args.origins else {
                    "scope": "Caller-supplied observations; origins were not provided or independently validated."
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
