#!/usr/bin/env python3
"""Bind authored representation operations to the native turn owner and Exchange."""

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import stat
import sys
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information.platform import (
    OPEN_RO_NOFOLLOW as _OPEN_RO_NOFOLLOW,
)
from managed_ai_information.platform import (
    atomic_bytes as _platform_atomic_bytes,
)
from managed_ai_information.platform import (
    check_unfollowed_ancestors as _check_unfollowed_ancestors,
)
from managed_ai_information.platform import (
    is_reparse_or_symlink as _is_reparse_or_symlink,
)
from managed_ai_information.platform import (
    strict_json_loads,
)

OPERATION = "g60-representation-operation/1"
WITNESS = "g60-representation-return/1"
BOUND_RETURN = "g60-representation-bound-return/1"
_MAX_INPUT_BYTES = 16 * 1024 * 1024


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def text(value: Any, name: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), name + " must be nonempty text")
    return value


def strings(value: Any, name: str) -> list[str]:
    require(
        isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
        name + " must be a list of nonempty strings",
    )
    require(len(value) == len(set(value)), name + " contains duplicates")
    return value


def digest(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def loads_strict(raw: bytes | bytearray | str) -> Any:
    payload: bytes | str = bytes(raw) if isinstance(raw, bytearray) else raw
    return strict_json_loads(payload, label="Representation JSON", require_object=False)


def _read_ordinary_bytes(path: Path | str, label: str, limit: int | None = None) -> bytes:
    cap = _MAX_INPUT_BYTES if limit is None else limit
    checked = _check_unfollowed_ancestors(path, label, include_target=True)
    require(
        checked.exists() and stat.S_ISREG(checked.lstat().st_mode),
        label + " must be an existing ordinary file",
    )
    fd = os.open(str(checked), _OPEN_RO_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(cap + 1)
    require(len(data) <= cap, f"{label} exceeds {cap} bytes")
    return data


def read_json(path: Path | str) -> Any:
    return loads_strict(_read_ordinary_bytes(path, "JSON input"))


def _atomic_bytes(path: Path, data: bytes) -> None:
    checked = _check_unfollowed_ancestors(path, "Target file", include_target=True)
    checked.parent.mkdir(parents=True, exist_ok=True)
    require(
        not _is_reparse_or_symlink(checked),
        "Target file must not be a symlink or reparse point: " + str(checked),
    )
    _platform_atomic_bytes(checked, data)


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Exact structural differences, not semantic effects or independence."""
    if type(before) is not type(after):
        return [prefix]

    if isinstance(before, dict):
        result: list[str] = []
        for key in sorted(set(before) | set(after)):
            at = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                result.append(at)
            else:
                result.extend(changed_paths(before[key], after[key], at))
        return result

    if isinstance(before, list):
        if len(before) != len(after):
            return [prefix]
        return [
            path
            for idx, (left, right) in enumerate(zip(before, after, strict=True))
            for path in changed_paths(left, right, prefix + "/" + str(idx))
        ]

    return [] if before == after else [prefix]


_COLOCATED_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_COLOCATED_SRC_ROOT = _COLOCATED_ENGINE_ROOT.parent


def _validate_engine_dir(engine: Path | str) -> Path:
    """Validate that engine points to an ordinary canonical src/managed_ai_information directory."""
    checked = _check_unfollowed_ancestors(engine, "Engine directory", include_target=True)
    if not checked.is_dir():
        raise ValueError("Engine directory does not exist: " + str(checked))

    resolved = checked.resolve()
    if (
        resolved.name != "managed_ai_information"
        or not (resolved / "__init__.py").is_file()
        or not (resolved / "store/system.py").is_file()
        or not (resolved / "core").is_dir()
    ):
        raise ValueError(
            "Engine directory must be a canonical src/managed_ai_information package directory: "
            + str(resolved),
        )
    return resolved


def _verify_loaded_from_engine(module: Any, resolved_engine: Path, expected_rel: str) -> None:
    loaded_file = getattr(module, "__file__", None)
    expected = (resolved_engine / expected_rel).resolve()
    if not loaded_file or Path(loaded_file).resolve() != expected:
        raise ValueError(
            f"Loaded module {getattr(module, '__name__', module)!r} did not come from "
            f"selected engine: {resolved_engine}",
        )


def _import_engine_api(resolved: Path, *, verify_all: bool) -> tuple[Any, ...]:
    sys_m: Any = importlib.import_module("managed_ai_information.store.system")
    sto_m: Any = importlib.import_module("managed_ai_information.core.storage")
    own_m: Any = importlib.import_module("managed_ai_information.core.ownership")
    inp_m: Any = importlib.import_module("managed_ai_information.store.protocol.inputs")
    pairs = (
        (sys_m, "store/system.py"),
        (sto_m, "core/storage.py"),
        (own_m, "core/ownership.py"),
        (inp_m, "store/protocol/inputs.py"),
    )
    for mod, rel in pairs if verify_all else pairs[:1]:
        _verify_loaded_from_engine(mod, resolved, rel)
    return (
        sys_m.System,
        sto_m.exclusive,
        own_m.NativeLayout,
        own_m.packet_id,
        inp_m.artifact_target,
    )


@contextmanager
def mount_engine(engine: Path | str) -> Generator[tuple[Any, ...]]:
    resolved = _validate_engine_dir(engine)
    if resolved == _COLOCATED_ENGINE_ROOT.resolve():
        yield _import_engine_api(resolved, verify_all=False)
        return

    saved_path = list(sys.path)
    saved_modules = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "managed_ai_information" or name.startswith("managed_ai_information.")
    }
    colocated_src = _COLOCATED_SRC_ROOT.resolve()

    def _is_colocated_src(entry: str) -> bool:
        try:
            return Path(entry or ".").resolve() == colocated_src
        except OSError:
            return False

    try:
        sys.path[:] = [
            str(resolved.parent),
            *(entry for entry in saved_path if not _is_colocated_src(entry)),
        ]
        importlib.invalidate_caches()
        pkg_spec = importlib.util.spec_from_file_location(
            "managed_ai_information",
            resolved / "__init__.py",
            submodule_search_locations=[str(resolved)],
        )
        if pkg_spec is None or pkg_spec.loader is None:
            raise ValueError("Unable to create module spec for external engine: " + str(resolved))

        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules["managed_ai_information"] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)
        yield _import_engine_api(resolved, verify_all=True)
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "managed_ai_information" or name.startswith("managed_ai_information."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        importlib.invalidate_caches()


def _load_engine_api(engine: Path) -> tuple[Any, Any, Any, Any, Any]:
    with mount_engine(engine) as api:
        return api


def _packet_id_fn() -> Any:
    return importlib.import_module("managed_ai_information.core.ownership").packet_id


def _artifact_target_fn() -> Any:
    return importlib.import_module("managed_ai_information.store.protocol.inputs").artifact_target


def _contract_path() -> Path:
    local = Path(__file__).resolve().with_name("CONTRACT.md")
    if local.is_file():
        return local
    return Path(__file__).resolve().parents[4] / "docs/companions/representation/CONTRACT.md"


class RepresentationOperations:
    """Use under the native cooperative owner lock, as the CLI does."""

    def __init__(self, system: Any) -> None:
        self.system = system
        self.ex = system.ex

    def location(self, turn: str) -> Path:
        return self.ex.path("representation-operations/" + _packet_id_fn()(turn))

    def _ordinary_workspace_file(self, name: str, error_message: str) -> Path:
        actual = self.ex.path(name)
        workspace_root = Path(self.ex.workspace)
        cur = workspace_root
        for part in Path(name).parts:
            cur = cur / part
            require(not _is_reparse_or_symlink(cur), error_message)

        require(actual.is_file() and not _is_reparse_or_symlink(actual), error_message)
        require(stat.S_ISREG(actual.lstat().st_mode), error_message)
        return actual

    def pin(self, path: str, accepted_in: str | None = None) -> dict[str, str]:
        name = _artifact_target_fn()(path)
        actual = self._ordinary_workspace_file(name, "pin requires an ordinary file: " + name)
        row = {"path": name, "sha256": digest(actual.read_bytes())}
        if accepted_in is not None:
            row["accepted_in"] = text(accepted_in, "accepted_in")
            self.check_pin(row, representation=True)
        return row

    def check_pin(self, row: Any, representation: bool = False) -> str:
        require(isinstance(row, dict), "input pin must be an object")
        name = text(row.get("path"), "input path")
        actual = self._ordinary_workspace_file(name, "input is not an ordinary file: " + name)
        data = actual.read_bytes()
        require(digest(data) == row.get("sha256"), "input basis changed: " + name)

        if representation:
            loads_strict(data)
            turn = text(row.get("accepted_in"), "representation accepted_in")
            observed = self.ex.inspect(turn)
            received = observed.get("return")
            require(received is not None, "representation acceptance has no native return")

            proposals = {prop["path"]: prop for prop in received["proposals"]}
            proposal = proposals.get(name)
            require(
                proposal is not None and proposal["sha256"] == row["sha256"],
                "representation differs from its nominated accepted proposal: " + name,
            )

            matching = [
                pub for pub in observed["publications"] if pub.get("record", {}).get("path") == name
            ]
            require(
                len(matching) == 1 and not matching[0].get("observation_error"),
                "representation acceptance is unavailable: " + name,
            )
            publication = matching[0]["record"]
            require(
                publication.get("phase") == "complete"
                and publication.get("sha256") == row["sha256"]
                and publication.get("return_sha256") == received["return_sha256"],
                "representation needs a completed matching native adoption: " + name,
            )

        return name

    def validate_operation(self, spec: Any) -> tuple[list[str], list[str]]:
        require(
            isinstance(spec, dict) and spec.get("format") == OPERATION,
            "unsupported operation format",
        )
        text(spec.get("turn"), "turn")
        mode = spec.get("mode")
        require(
            mode in ("develop", "transform", "realize"),
            "mode must be develop, transform or realize",
        )
        text(spec.get("task"), "task")
        text(spec.get("consumer"), "consumer")

        representations = spec.get("representations")
        sources = spec.get("sources")
        require(
            isinstance(representations, list) and isinstance(sources, list),
            "inputs must be explicit lists",
        )
        if mode != "develop":
            require(bool(representations), mode + " requires an accepted representation binding")

        rep_paths = [self.check_pin(item, representation=True) for item in representations]
        source_paths = [self.check_pin(item) for item in sources]
        require(
            len(rep_paths + source_paths) == len(set(rep_paths + source_paths)),
            "duplicate input paths",
        )

        targets = strings(spec.get("targets"), "targets")
        require(bool(targets), "operation requires a target")
        artifact_target = _artifact_target_fn()
        for target in targets:
            artifact_target(target)
            require(
                self.ex.path(target).parent.is_dir(), "target parent must already exist: " + target
            )

        applications = spec.get("applications")
        require(
            isinstance(applications, list) and bool(applications),
            "at least one authored application is required",
        )
        ids: list[str] = []
        for app in applications:
            require(isinstance(app, dict), "application must be an object")
            ids.append(text(app.get("id"), "application id"))
            used_sources = strings(app.get("sources"), "application sources")
            used_representations = strings(
                app.get("representations"), "application representations"
            )
            require(set(used_sources) <= set(source_paths), "application source was not selected")
            require(
                set(used_representations) <= set(rep_paths),
                "application representation was not selected",
            )
            if mode != "develop":
                require(
                    bool(used_representations), "application must bind an accepted representation"
                )
            text(app.get("operation"), "authored application operation")
            text(app.get("prediction"), "authored application prediction")

        require(len(ids) == len(set(ids)), "duplicate application identities")
        interactions = spec.get("interactions")
        require(isinstance(interactions, dict), "interactions must be explicitly supplied")
        text(interactions.get("account"), "interaction scope account")
        strings(interactions.get("unknowns"), "unknown interactions")
        return rep_paths, source_paths

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        reps, sources = self.validate_operation(spec)
        directory = self.location(spec["turn"])
        require(
            not directory.exists(),
            "operation identity already exists; inspect it, do not overwrite",
        )

        native = dict(spec.get("native", {}))
        require(
            not set(native) & {"task", "consumer", "sources", "targets"},
            "native additions cannot replace the bound task, consumer, sources or targets",
        )

        contract = _contract_path()
        require(contract.is_file(), "adapter contract is missing")
        contract_bytes = contract.read_bytes()
        operation_bytes = encoded(spec)
        directory.mkdir(parents=True)

        try:
            _atomic_bytes(directory / "operation.json", operation_bytes)
            _atomic_bytes(directory / "contract.md", contract_bytes)
            rel = directory.relative_to(self.ex.workspace).as_posix()
            native.update(
                task=spec["task"],
                consumer=spec["consumer"],
                targets=spec["targets"],
                sources=[*reps, *sources, rel + "/operation.json", rel + "/contract.md"],
            )
            result = self.system.context(spec["turn"], native)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        result["representation_operation"] = {
            "path": rel + "/operation.json",
            "sha256": digest(operation_bytes),
            "mode": spec["mode"],
            "adapter_sha256": digest(Path(__file__).read_bytes()),
            "scope": (
                "Exact selected artifact files; unknown interactions remain authored questions."
            ),
        }
        return result

    def operation(self, turn: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        """Read the operation from its native immutable capture, not a mutable index."""
        _, request = self.ex.load(turn)
        name = (self.location(turn) / "operation.json").relative_to(self.ex.workspace).as_posix()
        row = next((item for item in request["materials"] if item["path"] == name), None)
        if row is None:
            raise ValueError("native turn has no captured representation operation")
        raw = self.ex.get(row["sha256"])
        spec = loads_strict(raw)
        require(
            spec.get("format") == OPERATION and spec.get("turn") == turn,
            "captured operation identity changed",
        )
        return spec, row["sha256"], request

    def template(self, turn: str) -> dict[str, Any]:
        spec, key, _ = self.operation(turn)
        value: dict[str, Any] = {
            "format": WITNESS,
            "operation_sha256": key,
            "representations": spec["representations"],
            "applications": [
                {"id": app["id"], "account": "", "outputs": [], "remaining": []}
                for app in spec["applications"]
            ],
        }
        if spec["mode"] == "realize":
            value["realization"] = {"representations": spec["representations"], "account": ""}
        return value

    def receive(
        self,
        turn: str,
        reply: Path | str,
        witness: dict[str, Any],
        producer: str,
        proposals: list[str],
    ) -> dict[str, Any]:
        spec, key, request = self.operation(turn)
        targets = {target["path"]: target for target in request["targets"]}
        outputs: list[dict[str, Any]] = []

        for supplied in proposals:
            target, sep, source = supplied.partition("=")
            require(
                sep and target in targets and target not in {out["path"] for out in outputs},
                "proposal must name one unique captured target",
            )
            raw = _read_ordinary_bytes(source, "Proposal input")
            after = loads_strict(raw) if spec["mode"] != "realize" else None
            before_digest = targets[target]["sha256"]
            delta = None
            if spec["mode"] != "realize":
                before = loads_strict(self.ex.get(before_digest)) if before_digest else None
                delta = changed_paths(before, after)
            outputs.append(
                {
                    "path": target,
                    "before_sha256": before_digest,
                    "after_sha256": digest(raw),
                    "changed_json_pointers": delta,
                },
            )

        output_paths = {out["path"] for out in outputs}
        require(
            output_paths == set(targets),
            "every selected target needs an actual candidate; use a narrower turn otherwise",
        )
        self.validate_witness(spec, key, witness, output_paths)
        reply_text = _read_ordinary_bytes(reply, "Reply input").decode("utf-8")
        body = {
            "format": BOUND_RETURN,
            "operation_sha256": key,
            "inputs": {"representations": spec["representations"], "sources": spec["sources"]},
            "outputs": outputs,
            "authored_witness": witness,
            "actual_reply": reply_text,
            "scope": (
                "The author supplies meaning. Exact bindings and structural changes do not "
                "establish semantic adequacy."
            ),
        }
        reply_path = self.location(turn) / "bound-return.json"
        require(
            not reply_path.exists() and not _is_reparse_or_symlink(reply_path),
            "bound return already exists; inspect native return before recovery",
        )
        _atomic_bytes(reply_path, encoded(body))
        try:
            return self.system.receive(turn, reply_path, producer, proposals)
        except BaseException:
            ex_root = getattr(self.ex, "root", None)
            if not (
                ex_root is not None and (Path(ex_root) / "turns" / turn / "return.json").exists()
            ):
                reply_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def validate_witness(
        spec: dict[str, Any],
        key: str,
        witness: Any,
        output_paths: set[str],
    ) -> None:
        require(
            isinstance(witness, dict) and witness.get("format") == WITNESS,
            "unsupported witness format",
        )
        require(witness.get("operation_sha256") == key, "witness names a different operation basis")
        require(
            witness.get("representations") == spec["representations"],
            "witness representation basis is inconsistent",
        )

        expected_apps = {app["id"] for app in spec["applications"]}
        applications = witness.get("applications")
        require(
            isinstance(applications, list) and all(isinstance(app, dict) for app in applications),
            "application witnesses must be a list of objects",
        )
        require(
            len(applications) == len(expected_apps)
            and {app.get("id") for app in applications} == expected_apps,
            "missing or duplicate source-application witness",
        )

        for app in applications:
            text(app.get("account"), "actual application account")
            strings(app.get("remaining"), "remaining application questions")
            used = strings(app.get("outputs"), "application output bindings")
            require(
                bool(used) and set(used) <= output_paths,
                "application witness lacks an actual output binding",
            )

        require(
            set().union(*(set(app["outputs"]) for app in applications)) == output_paths,
            "an actual output lacks a source-application witness",
        )
        if spec["mode"] == "realize":
            realization = witness.get("realization")
            require(
                isinstance(realization, dict)
                and realization.get("representations") == spec["representations"],
                "realization lacks the exact accepted representation binding",
            )
            text(realization.get("account"), "actual realization account")

    def adopt(self, turn: str, target: str, reason: str) -> dict[str, Any]:
        spec, key, request = self.operation(turn)
        observed = self.ex.inspect(turn)
        received = observed.get("return")
        require(received is not None, "no native representation return")

        bound = loads_strict(self.ex.get(received["reply_sha256"]))
        require(
            bound.get("format") == BOUND_RETURN and bound.get("operation_sha256") == key,
            "native return lacks the representation application/realization binding",
        )
        require(
            bound.get("inputs")
            == {"representations": spec["representations"], "sources": spec["sources"]},
            "native return input bindings differ from the captured operation",
        )

        proposed = {prop["path"]: prop["sha256"] for prop in received["proposals"]}
        require(
            {out["path"]: out["after_sha256"] for out in bound["outputs"]} == proposed,
            "bound outputs differ from native received proposals",
        )

        captured_bases = {entry["path"]: entry["sha256"] for entry in request["targets"]}
        require(
            {out["path"]: out["before_sha256"] for out in bound["outputs"]} == captured_bases,
            "bound before-state differs from native captured targets",
        )

        self.validate_witness(spec, key, bound.get("authored_witness"), set(proposed))
        require(target in spec["targets"], "target outside representation operation")
        return self.system.adopt(turn, target, reason)


def run_cli_action(args: argparse.Namespace, *, engine_loader: Any = None) -> dict[str, Any]:
    """Execute one CLI action, constructing System only after acquiring owner.lock."""
    checked_engine = _check_unfollowed_ancestors(
        args.engine, "Engine directory", include_target=True
    )
    checked_root = _check_unfollowed_ancestors(args.root, "Native root", include_target=True)
    root = checked_root.expanduser().resolve()

    engine_ctx: AbstractContextManager[Any]
    if engine_loader is None or engine_loader is _load_engine_api:
        engine_ctx = mount_engine(checked_engine)
    else:
        engine_ctx = nullcontext(engine_loader(checked_engine))

    with engine_ctx as (System, exclusive, NativeLayout, _, _):
        exchange_root = NativeLayout(root).exchange.resolve()
        with exclusive(exchange_root / "owner.lock"):
            system = System(root)
            if system.ex.root.resolve() != exchange_root:
                raise ValueError("native exchange lock root differs from selected system exchange")
            adapter = RepresentationOperations(system)
            if args.action == "prepare":
                return adapter.prepare(read_json(args.spec))
            if args.action == "template":
                return adapter.template(args.turn)
            if args.action == "pin":
                return adapter.pin(args.path, args.accepted_in)
            if args.action == "receive":
                return adapter.receive(
                    args.turn,
                    args.reply,
                    read_json(args.witness),
                    args.producer,
                    args.proposal,
                )
            return adapter.adopt(args.turn, args.target, args.reason)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--engine",
        required=True,
        type=Path,
        metavar="ENGINE",
        help="canonical engine directory containing store/ and core/",
    )
    parser.add_argument("--root", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)

    actions.add_parser("prepare", allow_abbrev=False).add_argument(
        "--spec", required=True, type=Path
    )

    template = actions.add_parser("template", allow_abbrev=False)
    template.add_argument("turn")

    pin = actions.add_parser("pin", allow_abbrev=False)
    pin.add_argument("path")
    pin.add_argument("--accepted-in")

    receive = actions.add_parser("receive", allow_abbrev=False)
    receive.add_argument("turn")
    receive.add_argument("--reply", required=True, type=Path)
    receive.add_argument("--witness", required=True, type=Path)
    receive.add_argument("--producer", required=True)
    receive.add_argument("--proposal", action="append", default=[])

    adopt = actions.add_parser("adopt", allow_abbrev=False)
    adopt.add_argument("turn")
    adopt.add_argument("--target", required=True)
    adopt.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_cli_action(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        err = {"status": "unresolved", "operation": args.action, "error": str(exc)}
        print(json.dumps(err, allow_nan=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
