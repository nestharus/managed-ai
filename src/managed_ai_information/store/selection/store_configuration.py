"""Change the store's selected domains and catalogues against the reviewed descriptor."""

from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import REVIEWED_CONFIGURE_FORMAT as REVIEWED_CONFIGURE
from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import sha, strict_json_loads, write_json
from managed_ai_information.store.selection.domain_selection import DomainSelection, ordinary, pin

SELECTION_KEYS = ("domains", "catalogues")
LEGACY_CONFIGURE_ALLOWED = frozenset(
    {"expected_sha256", "account", "domains", "catalogues", "record_retention"}
)


def _reviewed_prior_digest(spec: dict[str, Any]) -> str:
    if spec["format"] != REVIEWED_CONFIGURE:
        raise ValueError("unsupported native configure format")
    # Omit the mandatory legacy guard at top level so old receivers refuse.
    if set(spec) != {"format", "prior", "selection", "account"}:
        raise ValueError("reviewed configure requires its own envelope")
    prior = spec["prior"]
    selection = spec["selection"]
    if not isinstance(prior, dict) or set(prior) != {"instance_id", "descriptor_sha256"}:
        raise ValueError("reviewed configure needs a prior instance and descriptor digest")
    if not isinstance(selection, dict) or set(selection) != set(SELECTION_KEYS):
        raise ValueError("reviewed configure needs ordered domain and catalogue bindings")
    return str(prior["descriptor_sha256"])


def _read_current_descriptor(root: Path, expected: Any, account: Any) -> dict[str, Any]:
    raw = ordinary(root / "system.json").read_bytes()
    if sha(raw) != expected:
        raise ValueError("store selection revision conflict")
    if not isinstance(account, str) or not account.strip():
        raise ValueError("selection change needs a receiving account")
    return dict(strict_json_loads(raw, root / "system.json", require_object=True))


def _refresh_reviewed_bindings(
    root: Path,
    current: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    instance = spec["prior"]["instance_id"]
    if not isinstance(instance, str) or packet_id(instance) != current.get("instance_id"):
        raise ValueError("reviewed configure receiving instance differs")
    for key in SELECTION_KEYS:
        reviewed = spec["selection"][key]
        if not isinstance(reviewed, list):
            raise ValueError("reviewed configure bindings must be ordered lists")
        actual = [
            {"path": binding["path"], "sha256": pin(root, root / binding["path"])["sha256"]}
            for binding in current.get(key, [])
        ]
        if len(reviewed) != len(actual):
            raise ValueError(
                f"reviewed {key} binding count differs: "
                f"expected {len(reviewed)}, current {len(actual)}"
            )
        for index, (wanted, observed) in enumerate(zip(reviewed, actual, strict=True), 1):
            if wanted != observed:
                raise ValueError(
                    f"reviewed {key} postimage differs at ordered row {index}: "
                    f"expected {wanted!r}, current {observed!r}"
                )
        current[key] = actual


def _apply_selection_changes(
    root: Path,
    current: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    extra = set(spec) - LEGACY_CONFIGURE_ALLOWED
    if extra:
        raise ValueError("unknown configure specification fields: " + ", ".join(sorted(extra)))
    if "record_retention" in spec:
        if spec["record_retention"] not in {"current-use", "append-history"}:
            raise ValueError("unsupported native record retention policy")
        current["record_retention"] = spec["record_retention"]
    for key in SELECTION_KEYS:
        if key in spec:
            current[key] = [pin(root, Path(path)) for path in spec[key]]


def configuration_descriptor(root: Path | str, spec: dict[str, Any]) -> dict[str, Any]:
    resolved_root = Path(root).resolve()
    if "format" in spec:
        expected = _reviewed_prior_digest(spec)
        current = _read_current_descriptor(resolved_root, expected, spec.get("account"))
        _refresh_reviewed_bindings(resolved_root, current, spec)
    else:
        current = _read_current_descriptor(
            resolved_root, spec["expected_sha256"], spec.get("account")
        )
        _apply_selection_changes(resolved_root, current, spec)
    selected = DomainSelection(resolved_root, current)
    current["schema_bindings"] = selected.schema_bindings
    current["selection_account"] = spec["account"]
    return current


def configure(system: Any, spec: dict[str, Any]) -> dict[str, Any]:
    descriptor = system.root / "system.json"
    current = configuration_descriptor(system.root, spec)
    selected = DomainSelection(system.root, current)
    from managed_ai_information.store.selection.installed_catalogue import InstalledCatalogue

    selected.validate_catalogue(
        {
            e["id"]: e
            for e in InstalledCatalogue(system.techniques.root, selected).index()["entries"]
        }
    )
    previous_kinds = system.persisted_descriptor.get(
        "schema_bindings", system.domain_selection.schemas
    )
    for record in system.record_store.all():
        if record["kind"] in previous_kinds and record["kind"] not in selected.schemas:
            raise ValueError("selected schema still owns stored records: " + record["kind"])
        selected.validate_record(record)
    write_json(descriptor, current)
    receipt = {
        "instance_id": system.instance_id,
        "descriptor_sha256": sha(descriptor.read_bytes()),
        "selection": selected.view(),
    }
    if spec.get("format") == REVIEWED_CONFIGURE:
        receipt["reviewed_plan"] = {
            "format": REVIEWED_CONFIGURE,
            "prior": spec["prior"],
            "selection": {key: current[key] for key in SELECTION_KEYS},
        }
    return receipt
