"""Read explicitly declared publication inputs without changing them.

Logical IDs bind to byte identities; filesystem locations are observation origins.
Recorded selections remain recorded observations. No discovery or source execution
is performed. See OBSERVER-INTERFACE.md for the declaration and assurance limits.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

DECLARATION_SCHEMA = "publication-declaration/1"
OBSERVATION_SCHEMA = "publication-observations/1"


def canonical_bytes(value):
    """The acquired source-service receipt's canonical UTF-8 JSON encoding."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _object(value, label):
    if not isinstance(value, dict) or any(not isinstance(k, str) or not k for k in value):
        raise ValueError(label + " must be an object with nonempty string keys")
    return value


def _keys(value, allowed, label):
    _object(value, label)
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError(label + " has unsupported fields: " + ", ".join(sorted(extra)))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


def _json(data, label):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(label + ": " + str(exc)) from exc


def _skipped(spec):
    _object(spec, "input specification")
    if "observe" in spec and not isinstance(spec["observe"], bool):
        raise ValueError("observe must be true or false")
    if spec.get("observe", True) is False:
        return {"status": "unobserved", "reason": "explicitly not observed"}
    return None


class _Reader:
    def __init__(self, roots, base_dir):
        self.roots = roots
        self.base_dir = Path(base_dir).resolve()
        self.cache = {}
        self.reads = []

    def read(self, binding):
        _keys(binding, ("root", "path"), "file binding")
        if not isinstance(binding.get("path"), str) or not binding["path"]:
            raise ValueError("file binding requires a nonempty path")
        address = Path(binding["path"])
        origin = {"kind": "file", "binding": copy.deepcopy(binding)}
        if "root" in binding:
            name = binding["root"]
            if not isinstance(name, str) or name not in self.roots:
                raise ValueError("unknown root: " + str(name))
            if address.is_absolute() or ".." in address.parts:
                raise ValueError("root-relative paths must not be absolute or contain '..'")
            root = Path(self.roots[name])
            if not root.is_absolute():
                root = self.base_dir / root
            root = root.resolve()
            origin["root_path"] = str(root)
            try:
                if not root.is_dir():
                    origin.update(status="unavailable", reason="configured root is not an available directory")
                    return None, origin
            except OSError as exc:
                origin.update(status="unavailable", reason="configured root could not be checked",
                              error={"type": type(exc).__name__, "message": str(exc)})
                return None, origin
            path = (root / address).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                raise ValueError("file binding resolves outside its configured root")
        else:
            path = (address if address.is_absolute() else self.base_dir / address).resolve()
        origin["path"] = str(path)
        key = str(path)
        if key not in self.cache:
            record = {"read_id": "read-%04d" % (len(self.reads) + 1), "path": key}
            try:
                data = path.read_bytes()
                record.update(status="observed", bytes=len(data), sha256=sha256(data))
            except FileNotFoundError:
                data = None
                record.update(status="absent", reason="named path was checked and is absent")
            except OSError as exc:
                data = None
                record.update(status="unavailable", reason="named path could not be read",
                              error={"type": type(exc).__name__, "message": str(exc)})
            self.reads.append(record)
            self.cache[key] = data, record
        data, record = self.cache[key]
        origin.update(copy.deepcopy(record))
        return data, origin

    def record(self, binding, expected=None):
        data, origin = self.read(binding)
        if data is None:
            return None, origin
        if expected is not None and (not isinstance(expected, str) or sha256(data) != expected):
            raise ValueError("recorded receipt bytes differ from the explicitly pinned receipt_sha256")
        value = _object(_json(data, "recorded receipt"), "recorded receipt")
        return value, origin


def _acceptance(reader, assertion, data):
    _keys(assertion, ("receipt", "artifact_path", "receipt_sha256"), "acceptance assertion")
    if not isinstance(assertion.get("artifact_path"), str) or not assertion["artifact_path"]:
        raise ValueError("acceptance assertion requires artifact_path")
    if "receipt" not in assertion:
        raise ValueError("acceptance assertion requires receipt")
    record, origin = reader.record(assertion["receipt"], assertion.get("receipt_sha256"))
    if record is None:
        return {"status": "unavailable", "receipt": origin,
                "reason": "the acceptance record was not available"}
    if record.get("accepted") is not True:
        raise ValueError("the supplied receipt does not assert accepted=true")
    after = _object(record.get("artifacts_after"), "receipt.artifacts_after")
    expected = _object(after.get(assertion["artifact_path"]), "accepted artifact entry")
    if expected.get("sha256") != sha256(data):
        raise ValueError("named artifact bytes differ from the supplied accepted occurrence")
    if "bytes" in expected and (type(expected["bytes"]) is not int or expected["bytes"] != len(data)):
        raise ValueError("accepted artifact byte count is contradictory")
    return {"status": "validated", "receipt": origin,
            "artifact_path": assertion["artifact_path"], "sha256": sha256(data),
            "bytes": len(data),
            "claim": "matches this supplied accepted=true record; receiving authority is supplied, not authenticated"}


def _selection(reader, spec):
    _keys(spec, ("ledger", "receipt_id", "receipt_sha256", "observe"), "selection specification")
    if "ledger" not in spec or not isinstance(spec.get("receipt_id"), str) or not spec["receipt_id"]:
        raise ValueError("selection requires a ledger binding and explicit receipt_id")
    ledger, ledger_origin = reader.record(spec["ledger"], spec.get("receipt_sha256"))
    if ledger is None:
        return None, {"status": "unavailable", "kind": "recorded_selection", "ledger": ledger_origin,
                      "reason": "a missing or unreadable ledger is not an observed absent selection"}
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
    if (not isinstance(policy_id, str) or inventory.get("selector_id") != policy_id or
            "selector" not in inventory or sha256(canonical_bytes(inventory["selector"])) != policy_id):
        raise ValueError("selection and inventory do not identify the same selector policy")
    if any(key not in selection for key in ("query", "query_interpretation", "limit")):
        raise ValueError("selected receipt is missing its complete recorded query basis")
    query = {key: copy.deepcopy(selection[key]) for key in ("query", "query_interpretation", "limit")}
    basis = {"query_sha256": sha256(canonical_bytes(query)),
             "catalogue_sha256": inventory_id, "policy_sha256": policy_id}
    origin = {"status": "observed", "kind": "recorded_selection", "ledger": ledger_origin,
              "selection_receipt_id": selection_id, "inventory_receipt_id": inventory_id,
              "query": query, "selector": copy.deepcopy(inventory["selector"]), "basis": copy.deepcopy(basis),
              "claim": "validated explicitly selected recorded basis; no new discovery or catalogue refresh"}
    for key in ("corpus_root", "claim", "eligible_count", "eligible_bytes"):
        if key in inventory:
            origin["recorded_" + key] = copy.deepcopy(inventory[key])
    return basis, origin


def observe(declaration: dict, roots=None, *, base_dir=None) -> dict:
    """Return current assessor maps plus origins; do not mutate the declaration.

    roots optionally overrides named root locations. Relative locations resolve
    against base_dir (the current directory by default). Only named paths are
    read. Missing keys remain unobserved; checked absent files become None.
    """
    _keys(declaration, ("schema", "description", "roots", "requirements", "sources", "artifacts", "selections"),
          "declaration")
    if declaration.get("schema") != DECLARATION_SCHEMA:
        raise ValueError("declaration.schema must be " + DECLARATION_SCHEMA)
    configured_roots = dict(_object(declaration.get("roots", {}), "declaration.roots"))
    if roots is not None:
        configured_roots.update(_object(roots, "root overrides"))
    for name, value in configured_roots.items():
        try:
            value = os.fspath(value)
        except TypeError as exc:
            raise ValueError("root locations must be filesystem paths") from exc
        if not isinstance(value, str) or not value:
            raise ValueError("root locations must be nonempty strings")
        configured_roots[name] = value
    reader = _Reader(configured_roots, Path.cwd() if base_dir is None else base_dir)
    current = {name: {} for name in ("requirements", "sources", "artifacts", "selections")}
    origins = {name: {} for name in current}
    for category in ("requirements", "sources", "artifacts"):
        for identity, spec in _object(declaration.get(category, {}), "declaration." + category).items():
            skipped = _skipped(spec)
            if skipped is not None:
                origins[category][identity] = skipped
                continue
            if category == "requirements" and "text" in spec:
                _keys(spec, ("text", "observe"), "inline requirement")
                if not isinstance(spec["text"], str):
                    raise ValueError("requirement text must be a string")
                data = spec["text"].encode("utf-8")
                digest = sha256(data)
                current[category][identity] = digest
                origins[category][identity] = {"status": "observed", "kind": "inline_utf8",
                                               "bytes": len(data), "sha256": digest}
                continue
            if category == "requirements" and "receipt" in spec:
                _keys(spec, ("receipt", "field", "receipt_sha256", "observe"), "recorded requirement")
                field = spec.get("field")
                if not isinstance(field, list) or not field or any(not isinstance(k, str) for k in field):
                    raise ValueError("recorded requirement field must be a nonempty array of object keys")
                record, record_origin = reader.record(spec["receipt"], spec.get("receipt_sha256"))
                if record is None:
                    origins[category][identity] = {"status": "unavailable", "kind": "recorded_requirement",
                        "receipt": record_origin, "field": copy.deepcopy(field)}
                    continue
                value = record
                for key in field:
                    if not isinstance(value, dict) or key not in value:
                        raise ValueError("recorded requirement field does not exist")
                    value = value[key]
                if not isinstance(value, str):
                    raise ValueError("recorded requirement field must contain a text body")
                data = value.encode("utf-8")
                digest = sha256(data)
                current[category][identity] = digest
                origins[category][identity] = {"status": "observed", "kind": "recorded_requirement",
                    "receipt": record_origin, "field": copy.deepcopy(field), "encoding": "utf-8",
                    "bytes": len(data), "sha256": digest}
                continue
            allowed = {"root", "path", "observe"}
            if category == "artifacts":
                allowed.add("acceptance")
            _keys(spec, allowed, category + " file specification")
            binding = {key: spec[key] for key in ("root", "path") if key in spec}
            data, origin = reader.read(binding)
            origins[category][identity] = origin
            if origin["status"] == "absent":
                current[category][identity] = None
            elif origin["status"] == "observed":
                if category == "requirements":
                    try:
                        data.decode("utf-8")
                    except UnicodeError as exc:
                        raise ValueError("requirement files must be valid UTF-8 text") from exc
                    origin["encoding"] = "utf-8 (exact original bytes, including newline/BOM)"
                current[category][identity] = sha256(data)
                if "acceptance" in spec:
                    origin["acceptance"] = _acceptance(reader, spec["acceptance"], data)
    for identity, spec in _object(declaration.get("selections", {}), "declaration.selections").items():
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
        "count_scope": "file payload reads in this invocation; root checks and caller-supplied content excluded"}
    origins["scope"] = ("Declared named-file byte observations and explicitly selected recorded receipt bases only. "
                        "No semantic dependency discovery, inventory refresh, source execution, acceptance authentication, "
                        "publication decision or obligation discharge. Each resolved file is read at most once per invocation; "
                        "the collection is not an atomic filesystem snapshot.")
    return {"schema": OBSERVATION_SCHEMA, "current": current, "origins": origins}


def observe_file(declaration_path, roots=None):
    """Read an explicit UTF-8 JSON declaration; relative paths use its directory."""
    path = Path(declaration_path).resolve()
    declaration = _json(path.read_bytes(), "declaration")
    return observe(declaration, roots=roots, base_dir=path.parent)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declaration", required=True)
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output", help="New JSON output file; omit for stdout")
    args = parser.parse_args(argv)
    try:
        overrides = {}
        for item in args.root:
            name, separator, path = item.partition("=")
            if not separator or not name or not path or name in overrides:
                raise ValueError("each --root requires a unique nonempty NAME=PATH")
            overrides[name] = str(Path(path).resolve())
        report = observe_file(args.declaration, roots=overrides)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with open(args.output, "x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print("publication_observer: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
