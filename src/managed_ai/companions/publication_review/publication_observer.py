"""Read explicitly declared publication inputs without changing them.

Logical IDs bind to byte identities; filesystem locations are observation origins.
Recorded selections remain recorded observations. No discovery or source execution
is performed. See docs/OBSERVATION-CONTRACT.md for the declaration and assurance limits.
"""

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.platform import (
    check_unfollowed_ancestors,
    strict_json_loads,
)
from managed_ai.platform import (
    is_reparse_or_symlink as _is_reparse_or_symlink,
)

DECLARATION_SCHEMA = "publication-declaration/1"
OBSERVATION_SCHEMA = "publication-observations/1"


def canonical_bytes(value: Any) -> bytes:
    """The acquired source-service receipt's canonical UTF-8 JSON encoding."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k for k in value):
        raise ValueError(label + " must be an object with nonempty string keys")
    return value


def _keys(value: Any, allowed: Any, label: str) -> None:
    _object(value, label)
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError(label + " has unsupported fields: " + ", ".join(sorted(extra)))


def _json(data: bytes, label: str) -> Any:
    try:
        return strict_json_loads(data, label=label, require_object=False)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(label + ": " + str(exc)) from exc


def _skipped(spec: Any) -> dict[str, str] | None:
    _object(spec, "input specification")
    if "observe" in spec and not isinstance(spec["observe"], bool):
        raise ValueError("observe must be true or false")
    if spec.get("observe", True) is False:
        return {"status": "unobserved", "reason": "explicitly not observed"}
    return None


def _validated_root_relative_parts(raw_path: Any) -> list[str]:
    """Validate a clean relative POSIX path without backslashes, drive prefixes, or '..'."""
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ValueError("file binding requires a nonempty path without NUL characters")
    if "\\" in raw_path:
        raise ValueError("root-relative paths must use forward slashes, not backslashes")
    if PureWindowsPath(raw_path).drive or (len(raw_path) >= 2 and raw_path[1] == ":"):
        raise ValueError("root-relative paths must not include a drive specifier")
    if PurePosixPath(raw_path).is_absolute():
        raise ValueError("root-relative paths must not be absolute or contain '..'")

    parts = raw_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("root-relative paths must not contain empty, '.', or '..' components")
    return parts


class _Reader:
    """Cached single-read observer enforcing root containment and unfollowed ancestors."""

    def __init__(self, roots: dict[str, str], base_dir: Path | str) -> None:
        self.roots = roots
        self.base_dir = Path(base_dir).resolve()
        self.cache: dict[str, tuple[bytes | None, dict[str, Any]]] = {}
        self.reads: list[dict[str, Any]] = []

    def _resolve_root_binding(
        self,
        name: Any,
        raw_path: str,
        origin: dict[str, Any],
    ) -> Path | None:
        if not isinstance(name, str) or name not in self.roots:
            raise ValueError("unknown root: " + str(name))

        parts = _validated_root_relative_parts(raw_path)
        raw_root = Path(self.roots[name])
        root_target = raw_root if raw_root.is_absolute() else self.base_dir / raw_root
        abs_root = check_unfollowed_ancestors(
            root_target,
            role="configured root " + name,
            include_target=True,
        )
        root = abs_root.resolve()
        origin["root_path"] = str(root)

        try:
            if not root.is_dir():
                origin.update(
                    status="unavailable",
                    reason="configured root is not an available directory",
                )
                return None
        except OSError as exc:
            origin.update(
                status="unavailable",
                reason="configured root could not be checked",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            return None

        current = root
        for part in parts:
            current = current / part
            if _is_reparse_or_symlink(current):
                raise ValueError(
                    "file binding traverses a symlink or reparse point under configured root: "
                    + raw_path,
                )

        path = current.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError("file binding resolves outside its configured root") from None
        return path

    def _read_cached_path(self, path: Path) -> tuple[bytes | None, dict[str, Any]]:
        key = str(path)
        if key not in self.cache:
            record: dict[str, Any] = {"read_id": f"read-{len(self.reads) + 1:04d}", "path": key}
            data: bytes | None = None
            try:
                raw_bytes = path.read_bytes()
                data = raw_bytes
                record.update(status="observed", bytes=len(raw_bytes), sha256=sha256(raw_bytes))
            except FileNotFoundError:
                record.update(status="absent", reason="named path was checked and is absent")
            except OSError as exc:
                record.update(
                    status="unavailable",
                    reason="named path could not be read",
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            self.reads.append(record)
            self.cache[key] = (data, record)
        return self.cache[key]

    def read(self, binding: dict[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
        _keys(binding, ("root", "path"), "file binding")
        raw_path = binding.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("file binding requires a nonempty path")

        origin: dict[str, Any] = {"kind": "file", "binding": copy.deepcopy(binding)}
        if "root" in binding:
            path = self._resolve_root_binding(binding["root"], raw_path, origin)
            if path is None:
                return None, origin
        else:
            address = Path(raw_path)
            path = (address if address.is_absolute() else self.base_dir / address).resolve()

        origin["path"] = str(path)
        data, record = self._read_cached_path(path)
        origin.update(copy.deepcopy(record))
        return data, origin

    def record(
        self,
        binding: dict[str, Any],
        expected: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        data, origin = self.read(binding)
        if data is None:
            return None, origin
        if expected is not None and (not isinstance(expected, str) or sha256(data) != expected):
            raise ValueError(
                "recorded receipt bytes differ from the explicitly pinned receipt_sha256"
            )
        value = _object(_json(data, "recorded receipt"), "recorded receipt")
        return value, origin


def _acceptance(reader: _Reader, assertion: dict[str, Any], data: bytes) -> dict[str, Any]:
    _keys(assertion, ("receipt", "artifact_path", "receipt_sha256"), "acceptance assertion")
    if not isinstance(assertion.get("artifact_path"), str) or not assertion["artifact_path"]:
        raise ValueError("acceptance assertion requires artifact_path")
    if "receipt" not in assertion:
        raise ValueError("acceptance assertion requires receipt")

    record, origin = reader.record(assertion["receipt"], assertion.get("receipt_sha256"))
    if record is None:
        return {
            "status": "unavailable",
            "receipt": origin,
            "reason": "the acceptance record was not available",
        }
    if record.get("accepted") is not True:
        raise ValueError("the supplied receipt does not assert accepted=true")

    after = _object(record.get("artifacts_after"), "receipt.artifacts_after")
    expected = _object(after.get(assertion["artifact_path"]), "accepted artifact entry")
    if expected.get("sha256") != sha256(data):
        raise ValueError("named artifact bytes differ from the supplied accepted occurrence")
    if "bytes" in expected and (
        type(expected["bytes"]) is not int or expected["bytes"] != len(data)
    ):
        raise ValueError("accepted artifact byte count is contradictory")

    return {
        "status": "validated",
        "receipt": origin,
        "artifact_path": assertion["artifact_path"],
        "sha256": sha256(data),
        "bytes": len(data),
        "claim": (
            "matches this supplied accepted=true record; receiving authority is supplied, not "
            "authenticated"
        ),
    }


def _selection(
    reader: _Reader, spec: dict[str, Any]
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    _keys(spec, ("ledger", "receipt_id", "receipt_sha256", "observe"), "selection specification")
    if (
        "ledger" not in spec
        or not isinstance(spec.get("receipt_id"), str)
        or not spec["receipt_id"]
    ):
        raise ValueError("selection requires a ledger binding and explicit receipt_id")

    ledger, ledger_origin = reader.record(spec["ledger"], spec.get("receipt_sha256"))
    if ledger is None:
        return None, {
            "status": "unavailable",
            "kind": "recorded_selection",
            "ledger": ledger_origin,
            "reason": "a missing or unreadable ledger is not an observed absent selection",
        }

    selections = _object(ledger.get("selections"), "ledger.selections")
    inventories = _object(ledger.get("inventories"), "ledger.inventories")
    selection_id = spec["receipt_id"]
    if selection_id not in selections:
        raise ValueError("explicit selection receipt_id is not present in the supplied ledger")

    selection = _object(selections[selection_id], "selection record")
    if sha256(canonical_bytes(selection)) != selection_id:
        raise ValueError("selection record does not match its content identity")

    inventory_id = selection.get("inventory_id")
    if not isinstance(inventory_id, str) or inventory_id not in inventories:
        raise ValueError("selected inventory record is missing or invalid")

    inventory = _object(inventories[inventory_id], "inventory record")
    if sha256(canonical_bytes(inventory)) != inventory_id:
        raise ValueError("inventory record does not match its content identity")

    policy_id = selection.get("selector_id")
    if (
        not isinstance(policy_id, str)
        or inventory.get("selector_id") != policy_id
        or "selector" not in inventory
        or sha256(canonical_bytes(inventory["selector"])) != policy_id
    ):
        raise ValueError("selection and inventory do not identify the same selector policy")

    if any(key not in selection for key in ("query", "query_interpretation", "limit")):
        raise ValueError("selected receipt is missing its complete recorded query basis")

    query = {
        key: copy.deepcopy(selection[key]) for key in ("query", "query_interpretation", "limit")
    }
    basis = {
        "query_sha256": sha256(canonical_bytes(query)),
        "catalogue_sha256": inventory_id,
        "policy_sha256": policy_id,
    }
    origin: dict[str, Any] = {
        "status": "observed",
        "kind": "recorded_selection",
        "ledger": ledger_origin,
        "selection_receipt_id": selection_id,
        "inventory_receipt_id": inventory_id,
        "query": query,
        "selector": copy.deepcopy(inventory["selector"]),
        "basis": copy.deepcopy(basis),
        "claim": (
            "validated explicitly selected recorded basis; no new discovery or catalogue refresh"
        ),
    }
    for key in ("corpus_root", "claim", "eligible_count", "eligible_bytes"):
        if key in inventory:
            origin["recorded_" + key] = copy.deepcopy(inventory[key])

    return basis, origin


def _normalize_roots(declaration: dict[str, Any], roots: Any) -> dict[str, str]:
    configured_roots = dict(_object(declaration.get("roots", {}), "declaration.roots"))
    if roots is not None:
        configured_roots.update(_object(roots, "root overrides"))

    normalized: dict[str, str] = {}
    for name, value in configured_roots.items():
        try:
            text_value = os.fspath(value)
        except TypeError as exc:
            raise ValueError("root locations must be filesystem paths") from exc
        if not isinstance(text_value, str) or not text_value:
            raise ValueError("root locations must be nonempty strings")
        normalized[name] = text_value
    return normalized


def _observe_inline_or_recorded_requirement(
    reader: _Reader,
    spec: dict[str, Any],
) -> tuple[str | None, dict[str, Any], bool]:
    if "text" in spec:
        _keys(spec, ("text", "observe"), "inline requirement")
        if not isinstance(spec["text"], str):
            raise ValueError("requirement text must be a string")
        data = spec["text"].encode("utf-8")
        digest = sha256(data)
        return (
            digest,
            {"status": "observed", "kind": "inline_utf8", "bytes": len(data), "sha256": digest},
            True,
        )

    if "receipt" in spec:
        _keys(spec, ("receipt", "field", "receipt_sha256", "observe"), "recorded requirement")
        field = spec.get("field")
        if not isinstance(field, list) or not field or any(not isinstance(k, str) for k in field):
            raise ValueError("recorded requirement field must be a nonempty array of object keys")

        record, record_origin = reader.record(spec["receipt"], spec.get("receipt_sha256"))
        if record is None:
            return (
                None,
                {
                    "status": "unavailable",
                    "kind": "recorded_requirement",
                    "receipt": record_origin,
                    "field": copy.deepcopy(field),
                },
                True,
            )

        value: Any = record
        for key in field:
            if not isinstance(value, dict) or key not in value:
                raise ValueError("recorded requirement field does not exist")
            value = value[key]
        if not isinstance(value, str):
            raise ValueError("recorded requirement field must contain a text body")

        data = value.encode("utf-8")
        digest = sha256(data)
        return (
            digest,
            {
                "status": "observed",
                "kind": "recorded_requirement",
                "receipt": record_origin,
                "field": copy.deepcopy(field),
                "encoding": "utf-8",
                "bytes": len(data),
                "sha256": digest,
            },
            True,
        )

    return None, {}, False


def _observe_file_entry(
    reader: _Reader,
    category: str,
    spec: dict[str, Any],
) -> tuple[Any, dict[str, Any], bool]:
    allowed = {"root", "path", "observe"}
    if category == "artifacts":
        allowed.add("acceptance")
    _keys(spec, allowed, category + " file specification")

    binding: dict[str, Any] = {key: spec[key] for key in ("root", "path") if key in spec}
    data, origin = reader.read(binding)
    if origin["status"] == "absent":
        return None, origin, True

    if origin["status"] == "observed" and data is not None:
        if category == "requirements":
            try:
                data.decode("utf-8")
            except UnicodeError as exc:
                raise ValueError("requirement files must be valid UTF-8 text") from exc
            origin["encoding"] = "utf-8 (exact original bytes, including newline/BOM)"
        if "acceptance" in spec:
            origin["acceptance"] = _acceptance(reader, spec["acceptance"], data)
        return sha256(data), origin, True

    return None, origin, False


def observe(
    declaration: dict[str, Any], roots: Any = None, *, base_dir: Path | str | None = None
) -> dict[str, Any]:
    """Return current assessor maps plus origins without mutating the declaration."""
    _keys(
        declaration,
        ("schema", "description", "roots", "requirements", "sources", "artifacts", "selections"),
        "declaration",
    )
    if declaration.get("schema") != DECLARATION_SCHEMA:
        raise ValueError("declaration.schema must be " + DECLARATION_SCHEMA)

    configured_roots = _normalize_roots(declaration, roots)
    reader = _Reader(configured_roots, Path.cwd() if base_dir is None else base_dir)
    current: dict[str, dict[str, Any]] = {
        name: {} for name in ("requirements", "sources", "artifacts", "selections")
    }
    origins: dict[str, Any] = {name: {} for name in current}

    for category in ("requirements", "sources", "artifacts"):
        for identity, spec in _object(
            declaration.get(category, {}), "declaration." + category
        ).items():
            skipped = _skipped(spec)
            if skipped is not None:
                origins[category][identity] = skipped
                continue

            if category == "requirements":
                req_digest, req_origin, handled = _observe_inline_or_recorded_requirement(
                    reader, spec
                )
                if handled:
                    origins[category][identity] = req_origin
                    if req_digest is not None:
                        current[category][identity] = req_digest
                    continue

            observed_value, origin, has_current = _observe_file_entry(reader, category, spec)
            origins[category][identity] = origin
            if has_current:
                current[category][identity] = observed_value

    for identity, spec in _object(
        declaration.get("selections", {}), "declaration.selections"
    ).items():
        skipped = _skipped(spec)
        if skipped is not None:
            origins["selections"][identity] = skipped
            continue
        basis, origin = _selection(reader, spec)
        origins["selections"][identity] = origin
        if basis is not None:
            current["selections"][identity] = basis

    origins["reads"] = copy.deepcopy(reader.reads)
    origins["acquisition"] = {
        "file_read_attempts": len(reader.reads),
        "files_read": sum(row["status"] == "observed" for row in reader.reads),
        "file_bytes_read": sum(row.get("bytes", 0) for row in reader.reads),
        "count_scope": (
            "file payload reads in this invocation; root checks and caller-supplied content "
            "excluded"
        ),
    }
    origins["scope"] = (
        "Declared named-file byte observations and explicitly selected recorded receipt bases "
        "only. "
        "No semantic dependency discovery, inventory refresh, source execution, acceptance "
        "authentication, "
        "publication decision or obligation discharge. Each resolved file is read at most once per "
        "invocation; "
        "the collection is not an atomic filesystem snapshot."
    )
    return {"schema": OBSERVATION_SCHEMA, "current": current, "origins": origins}


def observe_file(declaration_path: Path | str, roots: Any = None) -> dict[str, Any]:
    """Read an explicit UTF-8 JSON declaration; relative paths use its directory."""
    path = Path(declaration_path).resolve()
    declaration = _json(path.read_bytes(), "declaration")
    return observe(declaration, roots=roots, base_dir=path.parent)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--declaration", required=True)
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output", help="New JSON output file; omit for stdout")
    args = parser.parse_args(argv)

    try:
        overrides: dict[str, str] = {}
        for item in args.root:
            name, separator, path = item.partition("=")
            if not separator or not name or not path or name in overrides:
                raise ValueError("each --root requires a unique nonempty NAME=PATH")
            overrides[name] = os.path.abspath(path)

        report = observe_file(args.declaration, roots=overrides)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with open(args.output, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print("publication_observer: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
