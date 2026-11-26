"""CLI parser and command dispatcher for shared deployment selection."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from managed_ai_information import __version__
from managed_ai_information.core.storage import exclusive, is_reparse_or_symlink, shared

from .identity import _engine_directory, _ordinary_dir, _status_payload, read_descriptor_bytes
from .selection import (
    ActivationIncomplete,
    Deployment,
    DeploymentAdoptBasisChanged,
    DeploymentSelection,
    _check_required_domain,
    _precheck_local_system_args,
    observation_guard,
    report_admission,
    validate_deployment_sections,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Select one shared native engine and one schema-governed store by reference.",
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--deployment", type=Path, required=True)
    p.add_argument(
        "--expected-descriptor", help="Deployment edition verified by the forwarding entrance"
    )
    p.add_argument("--require-domain", help=argparse.SUPPRESS)
    p.add_argument("--admission-deadline", type=float, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init", allow_abbrev=False)
    init.add_argument("--runtime", "--store", dest="runtime", required=True, type=Path)
    init.add_argument("--engine", type=Path)

    sub.add_parser("status", allow_abbrev=False)
    sub.add_parser("verify", allow_abbrev=False)

    stage = sub.add_parser("stage", allow_abbrev=False)
    stage.add_argument("edition", nargs="?")
    stage.add_argument("--engine", required=True, type=Path)
    stage.add_argument(
        "--from-head",
        action="store_true",
        help="Derive edition name from the Git HEAD commit of --engine",
    )

    activate = sub.add_parser("activate", allow_abbrev=False)
    activate.add_argument("edition")
    activate.add_argument("--expected", required=True)
    activate.add_argument("--manifest-sha256", required=True)
    activate.add_argument("--reason", required=True)

    system = sub.add_parser("system", allow_abbrev=False)
    system.add_argument("arguments", nargs=argparse.REMAINDER)

    return p


def _run_explain_context(args: argparse.Namespace) -> None:
    root = _ordinary_dir(args.deployment)
    selected = Deployment(root)
    if (
        args.expected_descriptor is not None
        and selected.descriptor_sha256 != args.expected_descriptor
    ):
        raise ValueError("deployment descriptor differs")
    if args.require_domain is not None:
        _check_required_domain(selected.runtime, args.require_domain)

    result, app_args, ep = selected._dispatch(args.arguments, already_admitted=True)
    ep.emit(result, app_args)


def _run_system_command(args: argparse.Namespace) -> None:
    root = _ordinary_dir(args.deployment)
    descriptor = read_descriptor_bytes(root / "deployment.json")
    selection = DeploymentSelection(root, descriptor=descriptor)
    if (
        args.expected_descriptor is not None
        and selection.descriptor_sha256 != args.expected_descriptor
    ):
        raise ValueError("deployment descriptor differs")

    engine_cfg, store_cfg = validate_deployment_sections(selection.config)
    engine = _engine_directory(root / engine_cfg["path"])
    runtime = _ordinary_dir(root / store_cfg["path"])
    instance = store_cfg["instance_id"]
    read_only, extra_stores = _precheck_local_system_args(engine, runtime, args.arguments)

    with (
        report_admission(args.action),
        Deployment.admitted(
            root,
            engine=engine,
            instance=instance,
            deployment_sha=selection.descriptor_sha256,
            read_only=read_only,
            extra_stores=extra_stores,
            extra_read_only=bool(extra_stores),
            require_domain=args.require_domain,
            deadline=args.admission_deadline,
        ) as selected,
    ):
        result, app_args, ep = selected._dispatch(
            args.arguments,
            deadline=args.admission_deadline,
            already_admitted=True,
            proof_store_locked=bool(extra_stores),
        )

    ep.emit(result, app_args)


def _run_deployment_action(args: argparse.Namespace) -> dict[str, Any]:
    raw_root = args.deployment
    if args.action == "init":
        for existing in (raw_root, *raw_root.parents):
            try:
                if is_reparse_or_symlink(existing.lstat()):
                    raise ValueError(
                        f"ordinary deployment, engine and store directories required: {existing}"
                    )
            except FileNotFoundError:
                continue
        raw_root.mkdir(parents=True, exist_ok=True)

    root = _ordinary_dir(raw_root)
    lock = root / "deployment.lock"
    guard = shared if args.action in {"status", "verify"} else exclusive
    admission = observation_guard(guard, lock, deadline=args.admission_deadline)

    with report_admission(args.action), admission:
        if (
            args.expected_descriptor is not None
            and DeploymentSelection(root).descriptor_sha256 != args.expected_descriptor
        ):
            raise ValueError("deployment descriptor differs")

        if args.action == "init":
            return Deployment.initialize(root, args.runtime, args.engine)
        if args.action == "stage":
            return DeploymentSelection(root).stage(
                args.edition, args.engine, from_head=args.from_head
            )
        if args.action == "activate":
            return DeploymentSelection(root).activate(
                args.edition, args.expected, args.manifest_sha256, args.reason
            )

        selected = Deployment(root)
        if args.require_domain is not None:
            _check_required_domain(selected.runtime, args.require_domain)
        return _status_payload(
            selected.root,
            selected.engine,
            selected.runtime,
            selected.config,
            selected.descriptor_sha256,
        )


def main() -> None:
    args = parser().parse_args()
    try:
        if args.action == "system" and args.arguments[:1] == ["explain-context"]:
            _run_explain_context(args)
            return

        if args.action == "system":
            _run_system_command(args)
            return

        result = _run_deployment_action(args)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        failure: dict[str, Any] = {
            "status": "unresolved",
            "operation": args.action,
            "error": str(exc),
        }
        if isinstance(exc, ActivationIncomplete):
            failure.update(
                status="committed_incomplete",
                failed_step=exc.failed_step,
                descriptor_sha256=exc.descriptor_sha256,
            )
        elif isinstance(exc, DeploymentAdoptBasisChanged):
            failure["publication_basis"] = exc.basis

        print(json.dumps(failure), file=sys.stderr)
        raise SystemExit(2) from None
