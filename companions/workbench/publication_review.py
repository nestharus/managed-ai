"""Portable consumer of declared observations and the exact accepted assessor.

The source owner supplies dependency meaning. This module nominates review; it
never edits publications, discovers dependencies, or releases independent duties.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ASSESSOR_SHA256 = "3109ffeb01032d2f4dbc3afa6df2b05d9bb8a1f68f51561c86d2a7efbd6e1f67"
ASSESSOR_BYTES = 7100
HERE = Path(__file__).resolve().parent


def _load_assessor(path=None):
    """Load only the exact accepted module, without creating a bytecode cache."""
    path = Path(path) if path is not None else HERE / "continuation_assessor.py"
    body = path.read_bytes()
    if len(body) != ASSESSOR_BYTES or hashlib.sha256(body).hexdigest() != ASSESSOR_SHA256:
        raise ValueError("Assessor identity differs from the exact accepted module")
    spec = importlib.util.spec_from_file_location("publication_accepted_assessor", path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(body, str(path), "exec"), module.__dict__)
    return module


def assess_documents(previous, current, *, assessor_path=None):
    """Invoke the unchanged assessor directly and return its exact result.

    No files described by the documents are opened here. The assessor compares
    only declared identities and returns detached previous open obligations.
    Neither input document is mutated.
    """
    return _load_assessor(assessor_path).assess(previous, current)


def _load_observer():
    """Load the colocated installed producer, without creating bytecode files."""
    path = HERE / "publication_observer.py"
    spec = importlib.util.spec_from_file_location("publication_installed_observer", path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def review(previous, declaration, roots=None, *, base_dir=None, assessor_path=None):
    """Observe a declaration and assess it against the unmodified previous basis.

    Relative paths resolve from base_dir, defaulting to the current directory.
    roots overrides only locations, never the logical IDs or previous digests.
    The returned report retains the complete observation and exact assessment.
    """
    observation = _load_observer().observe(declaration, roots=roots, base_dir=base_dir)
    return review_observations(previous, observation, assessor_path=assessor_path)


def review_files(previous_path, declaration_path, roots=None, *, assessor_path=None):
    """Read explicit JSON inputs; declaration-relative paths use its directory."""
    previous = _read_json(previous_path)
    observation = _load_observer().observe_file(declaration_path, roots=roots)
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
        rows.append({"id": artifact["id"], "status": artifact["status"],
                     "explanation": meanings[artifact["status"]],
                     "reasons": copy.deepcopy(artifact["reasons"])})
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
    result = assess_documents(previous, observation["current"], assessor_path=assessor_path)
    return {
        "schema": "publication-review/1",
        "observation": copy.deepcopy(observation),
        "assessment": result,
        "current": copy.deepcopy(observation["current"]),
        "origins": copy.deepcopy(observation["origins"]),
        "explanations": explain(result),
        "assessor": {"bytes": ASSESSOR_BYTES, "sha256": ASSESSOR_SHA256},
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
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject,
                      object_pairs_hook=pairs)


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _retain(output_dir, previous, report):
    """Create one fresh explicitly requested occurrence; never overwrite one."""
    target = Path(output_dir)
    target.mkdir()  # Existing paths and missing parent directories are refused.
    values = {"previous.json": previous, "current.json": report["current"],
              "observed-origins.json": report["origins"],
              "observation.json": report["observation"],
              "assessment.json": report["assessment"], "report.json": report}
    for name, value in values.items():
        with (target / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_json_text(value))
    return str(target.resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", required=True, help="Previous assessor receipts JSON")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--declaration", help="Declared input bindings JSON; observe then assess")
    inputs.add_argument("--current", help="Current assessor observations JSON")
    inputs.add_argument("--observation", help="Producer JSON object containing current and origins")
    parser.add_argument("--origins", help="Origins JSON accompanying --current")
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH",
                        help="Override a named declaration root; repeat for different names")
    parser.add_argument("--assessor", help="Optional explicit path to the exact accepted module")
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
                overrides[name] = str(Path(location).resolve())
            observation = _load_observer().observe_file(args.declaration, roots=overrides)
        elif args.observation:
            observation = _read_json(args.observation)
        else:
            observation = {"current": _read_json(args.current),
                           "origins": _read_json(args.origins) if args.origins else {
                               "scope": "Caller-supplied observations; origins were not provided or independently validated."}}
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
