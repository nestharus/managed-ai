"""Deployment selection lifecycle, admission locks, and verified store dispatch."""

import io
import json
import os
import sys
from collections.abc import Callable, Generator
from contextlib import (
    AbstractContextManager,
    ExitStack,
    contextmanager,
    redirect_stderr,
    redirect_stdout,
)
from pathlib import Path
from typing import Any

from managed_ai_information.core.ownership import NativeLayout, external_root, packet_id
from managed_ai_information.core.storage import (
    AdmissionContended,
    admission_deadline,
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    exclusive,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    owner_admission,
    safe_relpath,
    sha,
    shared,
)

from . import identity as _identity
from .identity import (
    ENGINE_API,
    ENGINE_ROOT,
    FORMAT,
    VERSION,
    _descriptor_bytes,
    _descriptor_object,
    _engine_directory,
    _engine_record,
    _git_provenance,
    _ordinary_dir,
    _status_payload,
    read,
    read_descriptor,
    read_descriptor_bytes,
    runtime_binding,
)
from .isolation import _attach_deployment_metadata, _isolated_engine, _load_entrypoint


def _controller_attr(name: str, default: Any) -> Any:
    """Resolve a facade attribute from deployment.controller when patched in tests."""
    controller = sys.modules.get("managed_ai_information.deployment.controller")
    if controller is not None:
        value = getattr(controller, name, None)
        if value is not None:
            return value
    return default


def _resolve_code_identity(engine: Path) -> str:
    """Dispatch code_identity cleanly while honoring test patches on deployment.controller."""
    fn = _controller_attr("code_identity", _identity.code_identity)
    return fn(engine)


def validate_deployment_sections(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate that deployment.json engine and store sections are dicts with string paths
    (QA7-5).
    """
    engine_cfg = config.get("engine")
    store_cfg = config.get("store")

    if (
        not isinstance(engine_cfg, dict)
        or not isinstance(engine_cfg.get("path"), str)
        or not engine_cfg["path"]
    ):
        raise ValueError("invalid deployment descriptor: engine object with path string required")

    if (
        not isinstance(store_cfg, dict)
        or not isinstance(store_cfg.get("path"), str)
        or not store_cfg["path"]
    ):
        raise ValueError("invalid deployment descriptor: store object with path string required")

    if not isinstance(store_cfg.get("instance_id"), str) or not store_cfg["instance_id"]:
        raise ValueError("invalid deployment descriptor: store instance_id string required")

    return engine_cfg, store_cfg


class ActivationIncomplete(RuntimeError):
    def __init__(self, failed_step: str, descriptor_sha256: str, cause: BaseException) -> None:
        super().__init__(f"activation committed but {failed_step} failed: {cause}")
        self.failed_step = failed_step
        self.descriptor_sha256 = descriptor_sha256


class DeploymentAdoptBasisChanged(ValueError):
    def __init__(self, message: str, basis: dict[str, Any]) -> None:
        super().__init__(message)
        self.basis = basis


def observation_guard(
    guard: Callable[[Path], AbstractContextManager[Any]],
    path: Path,
    *,
    deadline: float | None = None,
) -> AbstractContextManager[Any]:
    return owner_admission(
        lambda guards: guards.enter_context(guard(path)),
        deadline=admission_deadline(deadline),
    )


@contextmanager
def report_admission(operation: str) -> Generator[None]:
    """Emit the CLI disposition only for an exhausted pre-operation admission."""
    try:
        yield
    except AdmissionContended as exc:
        print(json.dumps(exc.result(operation)), file=sys.stderr)
        raise SystemExit(75) from None


def _check_required_domain(runtime: Path, required_domain: str) -> None:
    """Verify the store descriptor pins the required domain by ordinary path and exact SHA-256."""
    sys_doc = read_descriptor(runtime / "system.json")
    domains = sys_doc.get("domains", ())
    if not isinstance(domains, list):
        raise ValueError("invalid domains declaration in store descriptor")

    matched = False
    for binding in domains:
        if (
            not isinstance(binding, dict)
            or not isinstance(binding.get("path"), str)
            or not isinstance(binding.get("sha256"), str)
        ):
            raise ValueError("invalid domain binding in store descriptor")

        ordinary_path = check_ordinary_ancestors(
            runtime / binding["path"],
            expect_dir=False,
            error_prefix="ordinary selected domain required",
        )
        raw = ordinary_path.read_bytes()
        if sha(raw) != binding["sha256"]:
            raise ValueError("selected domain manifest changed: " + str(ordinary_path))

        dom_doc = read_descriptor(ordinary_path, raw)
        if dom_doc.get("id") == required_domain:
            matched = True

    if not matched:
        raise ValueError(f"this entrance requires a {required_domain}-domain deployment")


def _extra_proof_stores(runtime: Path, parsed: Any) -> tuple[Path, ...]:
    if getattr(parsed, "action", None) != "recompose":
        return ()

    proof = getattr(parsed, "proof_store", None)
    if proof is None:
        return ()

    try:
        resolved = Path(proof).expanduser().resolve(strict=True)
    except OSError:
        return ()

    if resolved == Path(runtime).resolve():
        return ()

    lock_path = NativeLayout(resolved).exchange / "owner.lock"
    try:
        if not is_ordinary_file_stat(lock_path.lstat()):
            return ()
    except OSError:
        return ()

    return (resolved,)


def _precheck_local_system_args(
    engine: Path, runtime: Path, arguments: list[str]
) -> tuple[bool, tuple[Path, ...]]:
    """Parse arguments with THIS trusted controller's parser before locks (zero unverified
    execution).
    """
    if not arguments or any(v == "--root" or v.startswith("--root=") for v in arguments):
        raise ValueError("supply a native command; deployment owns its selected store")

    from managed_ai_information.store.cli.command_parser import is_readonly_action
    from managed_ai_information.store.cli.command_parser import parser as native_parser

    if engine != ENGINE_ROOT:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                parsed = native_parser().parse_args(["--root", str(runtime), *arguments])
        except SystemExit:
            return False, ()
        if parsed.action == "init":
            raise ValueError(
                "initialize a store through the central system entrance before deployment"
            )
        return is_readonly_action(parsed), _extra_proof_stores(runtime, parsed)

    parsed = native_parser().parse_args(["--root", str(runtime), *arguments])
    if parsed.action in ("show", "catalogue") and arguments[0] != parsed.action:
        raise ValueError("supply the native store observation as the command")
    if parsed.action == "init":
        raise ValueError("initialize a store through the central system entrance before deployment")
    return is_readonly_action(parsed), _extra_proof_stores(runtime, parsed)


class DeploymentSelection:
    """Read or replace deployment metadata, including when the incumbent code has drifted."""

    def __init__(self, root: Path | str, *, descriptor: bytes | None = None) -> None:
        self.root = _ordinary_dir(root)
        path = self.root / "deployment.json"
        raw = _descriptor_bytes(path) if descriptor is None else descriptor
        self.config = _descriptor_object(path, raw)
        if self.config.get("format") != FORMAT:
            raise ValueError(
                "copied-engine deployment is not supported; create a shared reference deployment"
            )
        self.descriptor_sha256 = sha(raw)

    def _require_current(self) -> None:
        if sha(_descriptor_bytes(self.root / "deployment.json")) != self.descriptor_sha256:
            raise ValueError("deployment selection revision conflict; reopen the deployment")

    def stage(
        self, edition: str | None, engine: Path | str, *, from_head: bool = False
    ) -> dict[str, Any]:
        self._require_current()
        engine_dir = _engine_directory(engine)

        if from_head:
            if edition is not None:
                raise ValueError("supply either an explicit edition or --from-head, not both")
            prov = _git_provenance(engine_dir)
            if prov is None:
                raise ValueError("stage --from-head requires an engine inside a Git checkout")
            suffix = "" if prov["git_clean"] else "-dirty"
            edition = f"commit-{str(prov['git_commit'])[:12]}{suffix}"

        if edition is None:
            raise ValueError("edition name or --from-head is required")
        packet_id(edition)

        store = self.config.get("store")
        if isinstance(store, dict) and isinstance(store.get("path"), str):
            store = dict(store, path=store["path"].replace("\\", "/"))

        code_sha = _resolve_code_identity(engine_dir)
        engine_rec = _engine_record(engine_dir, self.root, code_sha)
        value: dict[str, Any] = dict(self.config, edition=edition, engine=engine_rec)
        if store is not None:
            value["store"] = store

        encoded = encode(value)
        atomic_bytes(self.root / "candidate.json", encoded)
        result: dict[str, Any] = {
            "edition": edition,
            "candidate_sha256": sha(encoded),
            "engine": str(engine_dir),
        }
        if "git_commit" in engine_rec:
            result["engine_git_commit"] = engine_rec["git_commit"]
            result["engine_git_clean"] = engine_rec["git_clean"]
        return result

    def activate(
        self, edition: str, expected: str, manifest_sha: str, reason: str
    ) -> dict[str, Any]:
        if sha(_descriptor_bytes(self.root / "deployment.json")) != expected:
            raise ValueError("deployment selection revision conflict")

        candidate_path = self.root / "candidate.json"
        candidate_raw = _descriptor_bytes(candidate_path)
        candidate = _descriptor_object(candidate_path, candidate_raw)
        if candidate.get("edition") != edition or (
            sha(candidate_raw) != manifest_sha and sha(encode(candidate)) != manifest_sha
        ):
            raise ValueError("staged engine reference differs")

        if not reason.strip():
            raise ValueError("new code selection needs a compatibility/receiving account")

        engine_cfg, store_cfg = validate_deployment_sections(candidate)
        if candidate.get("format") != FORMAT or engine_cfg.get("engine_api") != ENGINE_API:
            raise ValueError("candidate declares an incompatible deployment/engine API")

        engine = _engine_directory(self.root / engine_cfg["path"])
        if _resolve_code_identity(engine) != engine_cfg.get("code_sha256"):
            raise ValueError("staged code changed")

        store_root = _ordinary_dir(self.root / store_cfg["path"])
        binding = runtime_binding(store_root)
        if any(store_cfg.get(k) != v for k, v in binding.items()):
            raise ValueError("candidate selects an incompatible or different store")

        candidate["account"] = reason
        descriptor_bytes = encode(candidate)
        descriptor_sha = sha(descriptor_bytes)
        atomic_bytes(self.root / "deployment.json", descriptor_bytes)

        step = "candidate_cleanup"
        try:
            candidate_path.unlink()
            step = "status_readback"
            readback_raw = _descriptor_bytes(self.root / "deployment.json")
            readback_cfg = _descriptor_object(self.root / "deployment.json", readback_raw)
            return _status_payload(self.root, engine, store_root, readback_cfg, sha(readback_raw))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ActivationIncomplete(step, descriptor_sha, exc) from exc


class Deployment(DeploymentSelection):
    """Open the selected engine and store after verifying their declared identities."""

    def __init__(self, root: Path | str, *, descriptor: bytes | None = None) -> None:
        super().__init__(root, descriptor=descriptor)
        engine_cfg, store_cfg = validate_deployment_sections(self.config)
        self.engine = _engine_directory(self.root / engine_cfg["path"])
        self.runtime = _ordinary_dir(self.root / store_cfg["path"])
        self._verify_binding()

    @classmethod
    def initialize(
        cls, root: Path | str, runtime: Path | str, engine: Path | str | None = None
    ) -> dict[str, Any]:
        raw_root = Path(root)
        for existing in (raw_root, *raw_root.parents):
            try:
                info = existing.lstat()
            except FileNotFoundError:
                continue
            if is_reparse_or_symlink(info):
                raise ValueError(
                    f"ordinary deployment, engine and store directories required: {existing}"
                )

        runtime_dir = _ordinary_dir(runtime)
        engine_dir = _engine_directory(engine or ENGINE_ROOT)
        resolved_root = Path(os.path.abspath(raw_root))

        if (
            resolved_root in (runtime_dir, engine_dir)
            or resolved_root.is_relative_to(runtime_dir)
            or resolved_root.is_relative_to(engine_dir)
        ):
            raise ValueError("deployment metadata must be separate from store and engine")

        if (resolved_root / "deployment.json").exists():
            raise ValueError(
                "deployment already exists; stage and activate an explicit new selection"
            )

        binding = runtime_binding(runtime_dir)
        raw_root.mkdir(parents=True, exist_ok=True)
        root_dir = _ordinary_dir(raw_root)
        code_sha = _resolve_code_identity(engine_dir)

        value = {
            "format": FORMAT,
            "controller_version": VERSION,
            "engine": _engine_record(engine_dir, root_dir, code_sha),
            "store": {"path": safe_relpath(runtime_dir, root_dir), **binding},
            "account": "Initial explicit shared engine/store selection.",
        }
        encoded = encode(value)
        atomic_bytes(root_dir / "deployment.json", encoded)
        return _status_payload(root_dir, engine_dir, runtime_dir, value, sha(encoded))

    def _verify_engine(self) -> None:
        engine_cfg, _ = validate_deployment_sections(self.config)
        if engine_cfg.get("engine_api") != ENGINE_API:
            raise ValueError("incompatible engine API")
        if _resolve_code_identity(self.engine) != engine_cfg.get("code_sha256"):
            raise ValueError(
                "shared engine code changed; qualify and activate its current identity"
            )

    def _verify_binding(self) -> dict[str, str]:
        _, store_cfg = validate_deployment_sections(self.config)
        binding = runtime_binding(self.runtime)
        if any(store_cfg.get(k) != v for k, v in binding.items()):
            raise ValueError("deployment selected a different store identity/schema")
        self._verify_engine()
        return binding

    def verify(self) -> dict[str, str]:
        self._require_current()
        return self._verify_binding()

    @classmethod
    @contextmanager
    def admitted(
        cls,
        root: Path | str,
        *,
        engine: Path | str | None = None,
        instance: str | None = None,
        deployment_sha: str | None = None,
        read_only: bool = True,
        store_sha: str | None = None,
        extra_stores: tuple[Path | str, ...] = (),
        extra_read_only: bool = False,
        require_domain: str | None = None,
        deadline: float | None = None,
    ) -> Generator[Deployment]:
        """Enter a pinned domain operation under shared deployment.lock, then sorted store
        owner.locks.
        """
        root_dir = _ordinary_dir(root)
        expected_engine = None if engine is None else _engine_directory(engine)
        extra_resolved = tuple(_ordinary_dir(p) for p in extra_stores)
        shared_guard = _controller_attr("shared", shared)
        exclusive_guard = _controller_attr("exclusive", exclusive)
        store_guard = shared_guard if read_only else exclusive_guard

        def acquire(guards: ExitStack) -> tuple[bytes, Path]:
            guards.enter_context(shared_guard(root_dir / "deployment.lock"))
            descriptor = read_descriptor_bytes(root_dir / "deployment.json")
            selection = DeploymentSelection(root_dir, descriptor=descriptor)
            if deployment_sha is not None and selection.descriptor_sha256 != deployment_sha:
                raise ValueError("deployment descriptor differs")

            engine_cfg, store_cfg = validate_deployment_sections(selection.config)
            actual_engine = _engine_directory(root_dir / engine_cfg["path"])
            if expected_engine is not None and actual_engine != expected_engine:
                raise ValueError("selected native engine differs")
            if instance is not None and store_cfg["instance_id"] != instance:
                raise ValueError("recipient instance differs")

            runtime = _ordinary_dir(root_dir / store_cfg["path"])
            all_stores = sorted({runtime, *extra_resolved}, key=lambda p: str(p))
            for store_root in all_stores:
                if store_root == runtime or not extra_read_only:
                    guards.enter_context(
                        store_guard(NativeLayout(store_root).exchange / "owner.lock")
                    )
                else:
                    guards.enter_context(
                        shared_guard(
                            NativeLayout(store_root).exchange / "owner.lock",
                            allow_readonly_open=True,
                        )
                    )
            return descriptor, runtime

        with owner_admission(acquire, deadline=admission_deadline(deadline)) as (
            descriptor,
            runtime,
        ):
            if (
                store_sha is not None
                and sha(read_descriptor_bytes(runtime / "system.json")) != store_sha
            ):
                raise ValueError("store descriptor differs")

            selected = cls(root_dir, descriptor=descriptor)
            if selected.runtime != runtime:
                raise ValueError("selected store path differs")
            if require_domain is not None:
                _check_required_domain(selected.runtime, require_domain)

            if selected.engine != ENGINE_ROOT:
                with _isolated_engine(selected.engine):
                    yield selected
            else:
                yield selected

    @classmethod
    def current_status(cls, root: Path | str) -> dict[str, Any]:
        selected = cls(root)
        return _status_payload(
            selected.root,
            selected.engine,
            selected.runtime,
            selected.config,
            selected.descriptor_sha256,
        )

    def status(self) -> dict[str, Any]:
        return Deployment.current_status(self.root)

    @classmethod
    def dispatch_current(cls, root: Path | str, arguments: list[str]) -> tuple[Any, Any, Any]:
        root_dir = _ordinary_dir(root)
        if arguments[:1] == ["explain-context"]:
            selected = cls(root_dir)
            return selected._dispatch(arguments, already_admitted=True)

        descriptor = read_descriptor_bytes(root_dir / "deployment.json")
        selection = DeploymentSelection(root_dir, descriptor=descriptor)
        engine_cfg, store_cfg = validate_deployment_sections(selection.config)
        engine = _engine_directory(root_dir / engine_cfg["path"])
        runtime = _ordinary_dir(root_dir / store_cfg["path"])
        instance = store_cfg["instance_id"]
        read_only, extra_stores = _precheck_local_system_args(engine, runtime, arguments)

        with cls.admitted(
            root_dir,
            engine=engine,
            instance=instance,
            deployment_sha=selection.descriptor_sha256,
            read_only=read_only,
            extra_stores=extra_stores,
            extra_read_only=bool(extra_stores),
        ) as bound:
            return bound._dispatch(
                arguments, already_admitted=True, proof_store_locked=bool(extra_stores)
            )

    def system(self, arguments: list[str]) -> tuple[Any, Any, Any]:
        self._require_current()
        if arguments[:1] == ["explain-context"]:
            self.verify()
            return self._dispatch(arguments, already_admitted=True)

        read_only, extra_stores = _precheck_local_system_args(self.engine, self.runtime, arguments)
        with type(self).admitted(
            self.root,
            engine=self.engine,
            instance=self.config["store"]["instance_id"],
            deployment_sha=self.descriptor_sha256,
            read_only=read_only,
            extra_stores=extra_stores,
            extra_read_only=bool(extra_stores),
        ) as bound:
            return bound._dispatch(
                arguments, already_admitted=True, proof_store_locked=bool(extra_stores)
            )

    def _dispatch(
        self,
        arguments: list[str],
        *,
        deadline: float | None = None,
        already_admitted: bool = False,
        proof_store_locked: bool = False,
    ) -> tuple[Any, Any, Any]:
        if not arguments or any(v == "--root" or v.startswith("--root=") for v in arguments):
            raise ValueError("supply a native command; deployment owns its selected store")

        if self.engine != ENGINE_ROOT:
            return self._selected_system(
                arguments,
                deadline,
                already_admitted=already_admitted,
                proof_store_locked=proof_store_locked,
            )
        return self._system_action(
            arguments,
            deadline,
            already_admitted=already_admitted,
            proof_store_locked=proof_store_locked,
        )

    def _native_entry(self) -> Path:
        return self.engine / _identity.NATIVE_ENTRY

    def _selected_system(
        self,
        arguments: list[str],
        deadline: float | None = None,
        *,
        already_admitted: bool = False,
        proof_store_locked: bool = False,
    ) -> tuple[Any, Any, Any]:
        with _isolated_engine(self.engine):
            return self._system_action(
                arguments,
                deadline,
                already_admitted=already_admitted,
                proof_store_locked=proof_store_locked,
            )

    def _system_action(
        self,
        arguments: list[str],
        deadline: float | None = None,
        *,
        already_admitted: bool = False,
        proof_store_locked: bool = False,
    ) -> tuple[Any, Any, Any]:
        ep = _load_entrypoint(self.engine)
        if ep.deployment_api != FORMAT or ep.engine_api != ENGINE_API:
            raise ValueError("selected engine does not implement the declared deployment API")

        args = ep.parser().parse_args(["--root", str(self.runtime), *arguments])
        if args.action in ("show", "catalogue") and arguments[0] != args.action:
            raise ValueError("supply the native store observation as the command")
        if args.action == "init":
            raise ValueError(
                "initialize a store through the central system entrance before deployment"
            )

        try:
            with self._guarded_system(
                ep, args, deadline, already_admitted=already_admitted
            ) as system:
                if proof_store_locked:
                    return ep.act(system, args, proof_store_locked=True), args, ep
                return ep.act(system, args), args, ep
        except ValueError as exc:
            if args.action == "adopt":
                basis = self._adopt_basis(ep, exc)
                if basis is not None:
                    raise DeploymentAdoptBasisChanged(str(exc), basis) from exc
            raise

    @staticmethod
    def _adopt_basis(ep: Any, exc: BaseException) -> dict[str, Any] | None:
        describe = getattr(ep, "adopt_basis", None)
        if describe is not None:
            try:
                basis = describe(exc)
                if type(basis) is dict:
                    json.dumps(basis, allow_nan=False)
                    return basis
            except OSError, ValueError, KeyError, TypeError, RuntimeError:
                pass
        return None

    @contextmanager
    def _guarded_system(
        self,
        ep: Any,
        args: Any,
        deadline: float | None = None,
        *,
        already_admitted: bool = False,
    ) -> Generator[Any]:
        if already_admitted:
            yield self._open_selected_store(ep, args)
            return

        if args.action == "explain-context":
            self.verify()
            yield self._open_selected_store(ep, args)
            return

        read_only = bool(ep.is_readonly_action(args))
        if args.action in {"show", "catalogue"}:
            with type(self).admitted(
                self.root,
                engine=self.engine,
                instance=self.config["store"]["instance_id"],
                deployment_sha=self.descriptor_sha256,
                read_only=read_only,
                deadline=deadline,
            ) as selected:
                yield selected._open_selected_store(ep, args)
            return

        layout = ep.native_layout(ep.external(self.runtime))
        exchange_root = external_root(layout.exchange)
        guard = (
            _controller_attr("shared", shared)
            if read_only
            else _controller_attr("exclusive", exclusive)
        )
        with observation_guard(guard, exchange_root / "owner.lock", deadline=deadline):
            self.verify()
            yield self._open_selected_store(ep, args)

    def _open_selected_store(self, ep: Any, args: Any) -> Any:
        override = (
            ep.configuration_descriptor(self.runtime, read(args.spec))
            if args.action == "configure"
            else None
        )
        system = ep.system_cls(self.runtime, _selection_override=override)
        _attach_deployment_metadata(system, self.engine, self.root, self.config)
        return system
