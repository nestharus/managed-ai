#!/usr/bin/env python3
"""Retain a bounded 12-process mechanical study; no model interpretation is simulated."""

import argparse
import json
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information.collective import PRODUCT, Collective
from managed_ai_information.platform import check_unfollowed_ancestors, is_reparse_or_symlink

COUNT = 12
AFFECTED = 5


def checked(result: dict[str, Any]) -> dict[str, Any]:
    if result["status"] != "accepted":
        raise RuntimeError(json.dumps(result, allow_nan=False))
    return result


def request(actor: str, command: str, operation: str, **args: Any) -> dict[str, Any]:
    return {"actor": actor, "command": command, "op": operation, **args}


def participant(database: str, index: int, ready: Any, release: Any, returns: Any) -> None:
    store = Collective(database)
    actor = f"peer-{index:02}"
    target = f"edge-{index:02}"

    context = checked(
        store.call(
            request(
                actor,
                "capture-original",
                "capture",
                id=f"context-{index:02}-original",
                target=target,
                sources=["shared-source"],
            ),
        ),
    )["context"]
    proposal = checked(
        store.call(
            request(
                actor,
                "retain-original",
                "propose",
                id=f"proposal-{index:02}-original",
                context=context["id"],
                body={"edge": index, "contribution": "scripted local fixture"},
            ),
        ),
    )["proposal"]

    ready.put(
        {
            "index": index,
            "pid": os.getpid(),
            "context": context["id"],
            "proposal": proposal["id"],
            "source_basis": context["basis"],
        },
    )
    if not release.wait(30):
        raise RuntimeError("Mechanical study did not release prepared participants")

    publication = request(
        actor,
        "publish-original",
        "publish",
        proposal=proposal["id"],
        account="Mechanical fixture admission; this is not a model judgment.",
    )
    result = store.call(publication)
    returns.put({"index": index, "pid": os.getpid(), "request": publication, "result": result})


def _seed_study_store(store: Collective) -> dict[str, Any]:
    """Seed the shared source and twelve peer edges for the mechanical study."""
    source = checked(
        store.call(
            request(
                "source-owner",
                "source",
                "create",
                id="shared-source",
                body={"text": "These source bytes remain unchanged throughout the study."},
            ),
        ),
    )["artifact"]

    for index in range(1, COUNT + 1):
        checked(
            store.call(
                request(
                    f"peer-{index:02}",
                    "seed",
                    "create",
                    id=f"edge-{index:02}",
                    body={"edge": index, "state": "initial"},
                    reconsider_from=["finding-reader"],
                ),
            ),
        )

    return source


def _run_participants(
    database: Path,
    store: Collective,
    source: dict[str, Any],
) -> tuple[
    list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]
]:
    """Spawn twelve concurrent participants and inject an impact offer before release."""
    mp_ctx = multiprocessing.get_context("spawn")
    ready = mp_ctx.Queue()
    returns = mp_ctx.Queue()
    release = mp_ctx.Event()
    processes = [
        mp_ctx.Process(
            target=participant,
            args=(str(database), index, ready, release, returns),
        )
        for index in range(1, COUNT + 1)
    ]

    for process in processes:
        process.start()
    try:
        prepared = [ready.get(timeout=30) for _ in processes]
        target = f"edge-{AFFECTED:02}"
        owner = f"peer-{AFFECTED:02}"
        impact_request = request(
            "finding-reader",
            "new-relation",
            "offer",
            target=target,
            basis=[source],
            account=(
                "Scripted mechanical fixture: a newly addressed interpretation reaches one use."
            ),
            body={
                "kind": "scripted-fixture",
                "semantic_discovery_claimed": False,
                "relation": {"source": "shared-source", "affected_use": target},
            },
        )
        impact = checked(store.call(impact_request))
        acknowledgment = checked(
            store.call(
                request(
                    owner,
                    "delivery-arrived",
                    "acknowledge",
                    notice=impact["notice"],
                    account="Delivery observed; this alone does not fulfill the receiving duty.",
                ),
            ),
        )
        release.set()
        outcomes = [returns.get(timeout=30) for _ in processes]
    finally:
        release.set()
        for process in processes:
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join()
            if process.exitcode != 0:
                raise RuntimeError(f"Worker failed: {process.exitcode}")

    return prepared, impact_request, impact, acknowledgment, outcomes


def _verify_first_publications(
    store: Collective,
    source: dict[str, Any],
    impact: dict[str, Any],
    acknowledgment: dict[str, Any],
    outcomes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Assert only the affected participant is refused while the notice stays pending."""
    accepted = [item for item in outcomes if item["result"]["status"] == "accepted"]
    refused = [item for item in outcomes if item["result"]["status"] == "refused"]
    if len(accepted) != COUNT - 1:
        raise RuntimeError(f"Unexpected accepted count: {len(accepted)}")
    if len(refused) != 1 or refused[0]["index"] != AFFECTED:
        raise RuntimeError("Expected only the affected participant to be refused")
    if refused[0]["result"]["code"] != "reconsideration-required":
        raise RuntimeError("Expected reconsideration-required refusal code")
    if (
        acknowledgment["state"] != "pending"
        or store.notice(impact["notice"])["receipt"] is not None
    ):
        raise RuntimeError("Delivery acknowledgment unexpectedly settled the required notice")
    if source != {key: store.show("shared-source")[key] for key in source}:
        raise RuntimeError("Shared source changed unexpectedly during study")
    return accepted, refused


def _run_receiving_phase(
    database: Path,
    store: Collective,
    impact: dict[str, Any],
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    """Capture, re-propose, publish, and replay exact receiving publication for the affected
    edge.
    """
    target = f"edge-{AFFECTED:02}"
    owner = f"peer-{AFFECTED:02}"
    original = store.retained("proposal", f"proposal-{AFFECTED:02}-original")

    current_context = checked(
        store.call(
            request(
                owner,
                "capture-reception",
                "capture",
                id="affected-reception-context",
                target=target,
                sources=["shared-source"],
            ),
        ),
    )["context"]
    current = checked(
        store.call(
            request(
                owner,
                "retain-reception",
                "propose",
                id="affected-reception-proposal",
                context=current_context["id"],
                body=original["body"],
            ),
        ),
    )["proposal"]

    publication = request(
        owner,
        "accept-reception",
        "publish",
        proposal=current["id"],
        treatments=[
            {
                "notice": impact["notice"],
                "disposition": "retained-adequate",
                "account": (
                    "Fixture receiving account keeps this body; actual semantic interpretation is "
                    "a "
                    "separate study."
                ),
            },
        ],
        account="Mechanical fixture checks revision and notice reception only.",
    )
    reception = checked(store.call(publication))
    recovered = Collective(database).call(publication)
    if (
        recovered != reception
        or store.show(target)["revision"] != 2
        or current["sha256"] != original["sha256"]
    ):
        raise RuntimeError("Receiving publication verification failed")
    return original, current_context, current, publication, reception, recovered


def run(directory: Path | str) -> dict[str, Any]:
    """Run the 12-process mechanical concurrency study in a fresh outside-product directory."""
    checked_dir = check_unfollowed_ancestors(directory, "Study directory", include_target=True)
    if checked_dir.exists() or is_reparse_or_symlink(checked_dir):
        raise ValueError("Select a new study directory outside the product tree")

    resolved_dir = checked_dir.expanduser().resolve()
    if resolved_dir.is_relative_to(PRODUCT):
        raise ValueError("Select a new study directory outside the product tree")

    resolved_dir.mkdir(parents=True)
    database = resolved_dir / "collective.json"
    Collective.initialize(database)
    store = Collective(database)

    source = _seed_study_store(store)
    prepared, impact_request, impact, acknowledgment, outcomes = _run_participants(
        database,
        store,
        source,
    )
    accepted, refused = _verify_first_publications(
        store,
        source,
        impact,
        acknowledgment,
        outcomes,
    )
    original, current_context, current, publication, reception, recovered = _run_receiving_phase(
        database,
        store,
        impact,
    )

    report = {
        "format": "g60-collective-mechanical-study/1",
        "scope": (
            "Twelve OS processes exercise local concurrency. No independent semantic agents, "
            "inferred relevance, or whole-product adequacy are claimed."
        ),
        "source": source,
        "prepared": sorted(prepared, key=lambda x: x["index"]),
        "impact_request": impact_request,
        "impact": impact,
        "delivery_acknowledgment": acknowledgment,
        "first_publications": sorted(outcomes, key=lambda x: x["index"]),
        "affected_original_proposal": original,
        "receiving_context": current_context,
        "receiving_proposal": current,
        "receiving_publication": {"request": publication, "result": reception},
        "recovered_exact_result": recovered,
        "final_notice": store.notice(impact["notice"]),
        "checks": {
            "distinct_worker_processes": len({x["pid"] for x in prepared}),
            "prepared_contributions_before_impact": len(prepared),
            "independent_publications_while_impact_pending": len(accepted),
            "only_affected_old_publication_refused": len(refused) == 1,
            "same_source_revision_and_bytes": True,
            "delivery_alone_left_obligation_pending": True,
            "original_proposal_retained": True,
            "unchanged_contribution_can_be_received_again": True,
            "exact_response_recovery_did_not_repeat_mutation": True,
            "whole_composition_semantic_acceptance": "not attempted by this mechanical exercise",
        },
        "database": str(database),
    }
    report_path = resolved_dir / "study.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {"report": str(report_path), "database": str(database), "checks": report["checks"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        result = run(args.directory)
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            json.dumps({"status": "unresolved", "error": str(exc)}, allow_nan=False),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
