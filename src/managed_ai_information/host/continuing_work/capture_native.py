#!/usr/bin/env python3
"""Host-owned selected capture for a qualified native implementation.

Canonical store/manage_store.py implementations qualify through:
  1. ordinary unfollowed paths across {deployment,core,store,platform} (no symlinks/junctions),
  2. verified store/system.py ENTRYPOINT contract (engine_api, deployment_api) and required
     public APIs (Attention.inspect_receipt, Exchange.packet_summary, System.assess), and
  3. co-located auto-admission (recording live profile SHA-256 and optional Git HEAD commit),
     OR explicit --expected-profile <sha256> / verified --deployment <dir> for external engines.

Frozen LEGACY_NATIVE_PROFILES remain strictly for historical pre-Wave-4 engine/runtime/ replay.
"""

import argparse
import importlib.util
import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[2]
if __package__ in (None, "") and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _CODE_ID_EXCLUDED_DIRS as _CODE_ID_EXCLUDED_DIRS,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _NON_SOURCE_SUFFIXES as _NON_SOURCE_SUFFIXES,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _SHA256_RE,
    EXTERNAL_PROFILE_ERROR,
    LEGACY_NATIVE_PROFILES,
    PROFILE_ERROR,
    RETURN_ERROR,
    _git_head_commit,
    _is_colocated_engine,
    _mount_canonical_module,
    _native_sources_with_layout,
    _profile_digest,
    _verify_canonical_contract,
    _verify_deployment_pin,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    COLOCATED_ENGINE_ENTRY as COLOCATED_ENGINE_ENTRY,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    COLOCATED_ENGINE_ROOT as COLOCATED_ENGINE_ROOT,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    CONTRACT_ERROR as CONTRACT_ERROR,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    EXPECTED_DEPLOYMENT_API as EXPECTED_DEPLOYMENT_API,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    EXPECTED_ENGINE_API as EXPECTED_ENGINE_API,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    EXPECTED_STORE_SCHEMA as EXPECTED_STORE_SCHEMA,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _collect_engine_entry as _collect_engine_entry,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _engine_code_identity as _engine_code_identity,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _is_test_module as _is_test_module,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _load_canonical_module as _load_canonical_module,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _mount_external_managed_ai_package as _mount_external_managed_ai_package,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _mount_legacy_engine_layout as _mount_legacy_engine_layout,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    _scan_ordinary_programs as _scan_ordinary_programs,
)
from managed_ai_information.host.continuing_work.native_engine_mount import (  # noqa: E402
    native_sources as native_sources,
)


@contextmanager
def qualified_engine(
    engine: Path,
    *,
    expected_profile: str | None = None,
    deployment: Path | None = None,
) -> Generator[Any]:
    """Qualify and mount a native implementation for the duration of an operation."""
    path, programs, layout = _native_sources_with_layout(engine)
    profile = _profile_digest(programs)
    resolved_entry = Path(engine).resolve(strict=True)
    engine_root = path.parents[1]

    if expected_profile is not None and (
        not isinstance(expected_profile, str) or not _SHA256_RE.fullmatch(expected_profile)
    ):
        raise ValueError("expected_profile must be a 64-character lowercase SHA-256 hex digest")

    if layout == "legacy":
        if profile not in LEGACY_NATIVE_PROFILES:
            raise ValueError(PROFILE_ERROR)
        if expected_profile is not None and profile != expected_profile:
            raise ValueError(PROFILE_ERROR)
        if deployment is not None:
            raise ValueError("Historical engine/runtime/ cannot use --deployment qualification")
        admission = "legacy-pinned"
    else:
        admission_modes: list[str] = []
        if expected_profile is not None:
            if profile != expected_profile:
                raise ValueError(PROFILE_ERROR)
            admission_modes.append("expected-profile")
        if deployment is not None:
            _verify_deployment_pin(deployment, engine_root)
            admission_modes.append("deployment-pin")
        if admission_modes:
            admission = "+".join(admission_modes)
        elif _is_colocated_engine(resolved_entry):
            admission = "colocated"
        else:
            raise ValueError(EXTERNAL_PROFILE_ERROR)

    info = {
        "profile": profile,
        "admission": admission,
        "git_head_commit": _git_head_commit(engine_root),
    }

    if layout == "canonical":
        with _mount_canonical_module(path, engine_root) as module:
            _verify_canonical_contract(module)
            yield module, info
        return

    spec = importlib.util.spec_from_file_location("reviewed_capture_engine", path)
    if spec is None or spec.loader is None:
        raise ValueError(PROFILE_ERROR)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module, info
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous


_qualified_engine = qualified_engine


def qualify_engine(
    engine: Path,
    *,
    expected_profile: str | None = None,
    deployment: Path | None = None,
) -> tuple[object, dict[str, Any]]:
    """Qualify and load a native implementation; refuse unqualified external engines before exec."""
    with qualified_engine(
        engine,
        expected_profile=expected_profile,
        deployment=deployment,
    ) as result:
        return result


def reviewed_engine(
    engine: Path,
    *,
    expected_profile: str | None = None,
    deployment: Path | None = None,
) -> tuple[object, str]:
    """Verify the selected native implementation before loading it."""
    module, info = qualify_engine(engine, expected_profile=expected_profile, deployment=deployment)
    return module, info["profile"]


def legacy_packet_summary(
    exchange: Any,
    directory: Path,
    consumer: str,
    publications: list[dict[str, Any]],
    returned: bool,
    outcome: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fallback packet summary for admitted predecessor engines predating
    Exchange.packet_summary.
    """
    attempted = exchange.transport.has_attempt(directory)
    transport = "returned" if returned else ("dispatch-attempted" if attempted else "prepared")
    row: dict[str, Any] = {
        "packet": directory.name,
        "consumer": consumer,
        "transport": transport,
        "publications": publications,
        "semantic_fulfillment": "not-decided-by-exchange",
    }
    if outcome is not None:
        row["transport"] = "terminal-without-return"
        row["transport_outcome"] = {k: outcome[k] for k in ("reason", "account", "outcome_sha256")}
    return row


def turn_summary(system: Any, turn: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project only the inspected packet without computing unrelated global status."""
    try:
        observed = system.ex.inspect(turn) if observed is None else observed
        publications: list[dict[str, Any]] = []
        for observation in observed["publications"]:
            if "record" not in observation:
                raise ValueError(observation["observation_error"])
            record = observation["record"]
            publications.append({"path": record["path"], "phase": record["phase"]})

        directory = system.ex.directory(turn)
        returned = observed["return"] is not None
        outcome = observed.get("transport_outcome")
        if hasattr(system.ex, "packet_summary"):
            return system.ex.packet_summary(
                directory,
                observed["consumer"],
                publications,
                returned=returned,
                outcome=outcome,
            )
        return legacy_packet_summary(
            system.ex,
            directory,
            observed["consumer"],
            publications,
            returned,
            outcome,
        )
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {"packet": turn, "transport": "record-unresolved", "error": str(exc)}


def _exchange_root(native: Any, root: Path) -> tuple[Path, Path]:
    resolved_root = (
        native.external(root) if hasattr(native, "external") else root.expanduser().resolve()
    )
    raw_exchange = (
        native.NativeLayout(resolved_root).exchange
        if hasattr(native, "NativeLayout")
        else resolved_root / "exchange"
    )
    exchange_root = (
        native.external(raw_exchange)
        if hasattr(native, "external")
        else raw_exchange.expanduser().resolve()
    )
    return resolved_root, exchange_root


def _lookup_attention_receipt(system: Any, raw_id: str) -> tuple[str, dict[str, Any], str | None]:
    if hasattr(system.attention, "inspect_receipt"):
        inspected = system.attention.inspect_receipt(raw_id)
        if inspected is None:
            raise ValueError("receipt identity not recorded in native Attention")
        return inspected["object"], inspected["record"], inspected.get("feedback_object")

    state = system.attention._load()
    current = system.attention._current(state)
    position = current["receipt_ids"].get(raw_id)
    if position is None:
        raise ValueError("receipt identity not recorded in native Attention")
    obj_key = state["receipts"][position]
    record = system.attention._get(obj_key)
    return obj_key, record, state["feedbacks"].get(record.get("feedback"))


def _reconcile_assessment(
    native: Any,
    system: Any,
    turn: str,
    receipt: Path,
    producer: str,
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Verify an already-committed native Attention receipt matches this exact capture request."""
    supplied = native.read(receipt)
    if not isinstance(supplied, dict) or set(supplied) != {"id", "assessments"}:
        raise ValueError("receipt must contain id and assessments")

    raw_id = supplied["id"]
    if not isinstance(raw_id, str):
        raise ValueError("receipt identity not recorded in native Attention")

    expected_source_sha = native.sha(native.encode(supplied))
    expected_feedback = native.feedback_id(turn)
    obj_key, record, current_feedback_obj = _lookup_attention_receipt(system, raw_id)
    rid = native.packet_id(raw_id)

    if (
        record.get("id") != rid
        or record.get("feedback") != expected_feedback
        or record.get("feedback_object") != current_feedback_obj
        or record.get("producer") != producer
        or record.get("source", {}).get("sha256") != expected_source_sha
    ):
        raise ValueError("receipt identity already used")

    attention = {
        "receipt": record["id"],
        "object": obj_key,
        "recorded": [item["item"] for item in record["assessments"]],
        "boundary": record["boundary"],
    }
    return {
        "attention": attention,
        "status": {"turns": [turn_summary(system, turn, observed)]},
        "reconciled": True,
    }


def _execute_capture_action(
    native: Any,
    system: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.action == "inspect-turn":
        return {"turn": turn_summary(system, args.turn)}
    if args.action == "reconcile-assess":
        observed = system.ex.inspect(args.turn)
        return _reconcile_assessment(
            native,
            system,
            args.turn,
            args.receipt,
            args.producer,
            observed,
        )
    observed = system.ex.inspect(args.turn)
    if observed["return"] is not None:
        raise ValueError(RETURN_ERROR)
    attention = system.assess(args.turn, args.receipt, args.producer)
    return {
        "attention": attention,
        "status": {"turns": [turn_summary(system, args.turn, observed)]},
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument(
        "--expected-profile",
        default=None,
        help="Explicit 64-hex SHA-256 profile pin for an external native engine",
    )
    parser.add_argument(
        "--deployment",
        default=None,
        type=Path,
        help="Verified deployment directory whose deployment.json pins the external engine",
    )

    commands = parser.add_subparsers(dest="action", required=True)
    capture = commands.add_parser("assess", allow_abbrev=False)
    capture.add_argument("turn")
    capture.add_argument("--receipt", required=True, type=Path)
    capture.add_argument("--producer", required=True)

    reconcile = commands.add_parser(
        "reconcile-assess",
        allow_abbrev=False,
        help="verify and recover an already-committed assessment receipt",
    )
    reconcile.add_argument("turn")
    reconcile.add_argument("--receipt", required=True, type=Path)
    reconcile.add_argument("--producer", required=True)

    inspect = commands.add_parser(
        "inspect-turn",
        allow_abbrev=False,
        help="read-only compatibility observation",
    )
    inspect.add_argument("turn")
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for qualified native capture and reconciliation."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        with qualified_engine(
            args.engine,
            expected_profile=args.expected_profile,
            deployment=args.deployment,
        ) as (native, info):
            root, exchange_root = _exchange_root(native, args.root)
            with native.exclusive(exchange_root / "owner.lock"):
                system = native.System(root)
                if system.ex.root.resolve() != exchange_root.resolve():
                    raise ValueError(
                        "native exchange lock root differs from selected system exchange"
                    )

                result = _execute_capture_action(native, system, args)

                adapter_meta: dict[str, Any] = {
                    "native_profile_sha256": info["profile"],
                    "admission": info["admission"],
                    "scope": (
                        "Selected native assessment and turn only; no global status computation."
                    ),
                }
                if info.get("git_head_commit") is not None:
                    adapter_meta["git_head_commit"] = info["git_head_commit"]
                result["capture_adapter"] = adapter_meta

        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(
            json.dumps(
                {
                    "status": "unresolved",
                    "operation": args.action,
                    "error": str(exc),
                    **getattr(exc, "details", {}),
                },
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
