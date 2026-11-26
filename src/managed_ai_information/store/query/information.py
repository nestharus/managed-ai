"""Optional exact interpretation bindings and explicit information changes.

The owner supplies meanings and accepts the proposed payload. This module checks
declared identities and nominated preservation; it is not a schema DSL, semantic
compatibility proof, arbitrary-code runner or multi-record transaction service.
The app caller holds the ordinary runtime owner lock around mutations.
"""

import json
import re
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import INFORMATION_FORMAT as FORMAT
from managed_ai_information.core.formats import STORE_FORMAT as RECORD_FORMAT
from managed_ai_information.core.storage import encode, sha
from managed_ai_information.store.protocol.identities import (
    digest,
    exact_fields,
    identity,
    nonempty,
    record_identity,
    require,
    strict_json,
)
from managed_ai_information.store.protocol.inputs import write_input

NAMESPACE = "g60-information/"
CONTRACT_KIND = "content-contract"
RESERVED_IDS = frozenset({"goal", "field", "frontier"})
RESERVED_KINDS = frozenset({"goal", "obligation", "field", "frontier", CONTRACT_KIND})


def pointer(value: Any, path: str) -> Any:
    """Resolve an explicit JSON pointer; a missing value never becomes a default."""
    require(isinstance(path, str), "preservation pointer must be text")
    if path == "":
        return value
    require(path.startswith("/"), "preservation pointer must be empty or start with /")
    for token in path[1:].split("/"):
        require(re.search(r"~(?![01])", token) is None, "invalid preservation pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            require(key in value, "preservation pointer is absent: " + path)
            value = value[key]
        elif isinstance(value, list):
            require(
                re.fullmatch(r"0|[1-9][0-9]*", key) is not None,
                "preservation array pointer must be a nonnegative index",
            )
            require(int(key) < len(value), "preservation pointer is absent: " + path)
            value = value[int(key)]
        else:
            raise ValueError("preservation pointer has no child: " + path)
    return value


class InformationEnvelope:
    """Validate retained interpretation and migration evidence during every record write."""

    def __init__(self, objects: Any) -> None:
        self.objects = objects

    @staticmethod
    def _eligible(record: dict[str, Any]) -> None:
        require(
            record.get("id") not in RESERVED_IDS and record.get("kind") not in RESERVED_KINDS,
            "this built-in record owner has no information-envelope adapter",
        )

    @staticmethod
    def _marked(content: Any) -> bool:
        return (
            isinstance(content, dict)
            and isinstance(content.get("format"), str)
            and content["format"].startswith(NAMESPACE)
        )

    def _object_record(self, pin: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        identity(pin, object_pin=True)
        data = self.objects.read(pin["object"])
        record = strict_json(data)
        require(record.get("format") == RECORD_FORMAT, "unsupported retained record format")
        require(
            record_identity(record) == {k: pin[k] for k in ("id", "revision", "sha256")},
            "retained record differs from its information pin",
        )
        require(
            record["sha256"] == sha(encode({k: v for k, v in record.items() if k != "sha256"})),
            "retained information record digest changed",
        )
        return record, data

    def _definition(self, pin: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        record, data = self._object_record(pin)
        require(
            record.get("kind") == CONTRACT_KIND and record.get("id") not in RESERVED_IDS,
            "interpretation source must be a content-contract record",
        )
        require(
            isinstance(record.get("content"), (dict, str)) and bool(record["content"]),
            "content-contract must supply a nonempty prose or JSON definition",
        )
        require(
            not self._marked(record["content"]),
            "content-contract definitions cannot recursively wrap themselves",
        )
        return record, data

    def _envelope(self, record: dict[str, Any]) -> dict[str, Any] | None:
        content = record.get("content")
        if not isinstance(content, dict) or not self._marked(content):
            return None
        self._eligible(record)
        require(content["format"] == FORMAT, "unsupported information envelope version")
        exact_fields(
            content, {"format", "contract", "payload"}, {"derivation"}, "information envelope"
        )
        self._definition(content["contract"])
        # Payload is deliberately open JSON. JSON equality below preserves the
        # distinction between Boolean, integer, floating-point and null values.
        json.dumps(content["payload"], allow_nan=False)
        if "derivation" in content:
            derivation = content["derivation"]
            exact_fields(
                derivation, {"operation", "source", "spec_object"}, set(), "information derivation"
            )
            require(
                derivation["operation"] in {"wrap", "migrate"},
                "unknown information derivation operation",
            )
            source, _ = self._object_record(derivation["source"])
            require(
                source["id"] == record["id"], "information derivation belongs to another record"
            )
            digest(derivation["spec_object"], "information change specification")
            spec = strict_json(self.objects.read(derivation["spec_object"]))
            require(
                spec.get("source") == record_identity(source),
                "information source differs from its retained specification",
            )
            require(
                spec.get("contract")
                == {k: content["contract"][k] for k in ("id", "revision", "sha256")},
                "information contract differs from its retained specification",
            )
        return content

    def validate_write(
        self,
        record: dict[str, Any],
        previous: dict[str, Any] | None,
        *,
        mode: str | None = None,
    ) -> None:
        """Called by the aware owner's put; raw older engines do not enforce it."""
        require(mode in {None, "wrap", "migrate"}, "unknown information write mode")
        with self.objects.pack_session():
            incoming = self._envelope(record)
            old = self._envelope(previous) if previous is not None else None
        if old is not None:
            if incoming is None:
                raise ValueError(
                    "information contract removal requires an explicit supported migration"
                )
            if incoming["contract"] != old["contract"]:
                require(mode == "migrate", "information contract switch requires explicit migrate")
        elif previous is not None and incoming is not None:
            require(mode == "wrap", "attaching an information contract requires explicit wrap")


class Information(InformationEnvelope):
    """Explicit information operations over the native record owner."""

    def __init__(self, system: Any) -> None:
        self.records = system.record_store
        super().__init__(self.records.objects)

    def _contract_pin(self, wanted: dict) -> dict:
        pin = self.records.pin(self.records.head(wanted))
        self._definition(pin)
        return pin

    def _basis(self, source: dict, spec: dict) -> list[str]:
        if "basis" in spec:
            value = spec["basis"]
            require(
                isinstance(value, list)
                and all(isinstance(v, str) and v for v in value)
                and len(set(value)) == len(value),
                "explicit information basis must be unique workspace paths",
            )
            return value
        require(
            not self.records.basis_changes(source),
            "information source basis changed; explicitly review and supply the successor basis",
        )
        return [item["path"] for item in source["basis"]]

    def _write(self, source: dict, content: dict, basis: list[str], mode: str) -> dict:
        record = {key: source[key] for key in ("id", "kind", "relations", "active")}
        record.update(content=content, basis=basis)
        result = self.records.put_information(record, source["revision"], mode)
        return {
            "record": result,
            "information": content,
            "scope": (
                "Exact source/definition binding and nominated preservation. "
                "Meanings, unknowns and adequacy are supplied by the accepting owner."
            ),
        }

    def wrap(self, spec: dict) -> dict:
        exact_fields(
            spec, {"source", "contract", "account"}, {"basis"}, "information wrap specification"
        )
        nonempty(spec["account"], "information wrapping account")
        source = self.records.head(spec["source"])
        self._eligible(source)
        require(
            self._envelope(source) is None,
            "source is already wrapped; use explicit migrate for a contract change",
        )
        basis = self._basis(source, spec)
        contract = self._contract_pin(spec["contract"])
        content = {
            "format": FORMAT,
            "contract": contract,
            "payload": source["content"],
            "derivation": {
                "operation": "wrap",
                "source": self.records.pin(source),
                "spec_object": self.objects.put(encode(spec)),
            },
        }
        return self._write(source, content, basis, "wrap")

    def migrate(self, spec: dict) -> dict:
        exact_fields(
            spec,
            {"source", "contract", "payload", "preserve", "change", "unknowns"},
            {"basis"},
            "information migration specification",
        )
        nonempty(spec["change"], "information change account")
        nonempty(spec["unknowns"], "information unknown-values account")
        json.dumps(spec["payload"], allow_nan=False)
        source = self.records.head(spec["source"])
        self._eligible(source)
        current = self._envelope(source)
        if current is None:
            raise ValueError(
                "migration requires a wrapped source; use wrap to declare its first interpretation"
            )
        require(isinstance(spec["preserve"], list), "preservation nominations must be a list")
        seen = set()
        for item in spec["preserve"]:
            exact_fields(item, {"from", "to"}, set(), "preservation nomination")
            before, after = (
                pointer(current["payload"], item["from"]),
                pointer(spec["payload"], item["to"]),
            )
            require((item["from"], item["to"]) not in seen, "duplicate preservation nomination")
            seen.add((item["from"], item["to"]))
            require(
                encode(before) == encode(after),
                "nominated information preservation failed: " + item["from"] + " -> " + item["to"],
            )
        basis = self._basis(source, spec)
        contract = self._contract_pin(spec["contract"])
        content = {
            "format": FORMAT,
            "contract": contract,
            "payload": spec["payload"],
            "derivation": {
                "operation": "migrate",
                "source": self.records.pin(source),
                "spec_object": self.objects.put(encode(spec)),
            },
        }
        return self._write(source, content, basis, "migrate")

    def inspect(self, ident: str) -> dict:
        record = self.records.show(ident)
        with self.objects.pack_session():
            envelope = self._envelope(record)
            if envelope is None:
                return {
                    "record": record,
                    "contracted": False,
                    "scope": "Ordinary open content; no optional information contract is declared.",
                }
            definition, _ = self._definition(envelope["contract"])
            result = {
                "record": record,
                "contracted": True,
                "definition": definition,
                "scope": (
                    "The exact pinned definition is exposed; "
                    "no latest-definition substitution or semantic validation is implied."
                ),
            }
            if "derivation" in envelope:
                result["last_explicit_conversion"] = strict_json(
                    self.objects.read(envelope["derivation"]["spec_object"])
                )
                result["conversion_scope"] = (
                    "This is the envelope lineage conversion. "
                    "Later same-contract payload edits remain ordinary record revisions."
                )
            return result

    def context_inputs(self, directory: Path, selected: list[dict]) -> dict:
        paths, exposed, by_object = [], [], {}
        with self.objects.pack_session():
            for record in selected:
                envelope = self._envelope(record)
                if envelope is None:
                    continue
                pin = envelope["contract"]
                _definition, body = self._definition(pin)
                if pin["object"] not in by_object:
                    path = write_input(
                        self.records.work, directory, "information/" + pin["object"] + ".json", body
                    )
                    by_object[pin["object"]] = path
                    paths.append(path)
                exposed.append(
                    {
                        "record": record_identity(record),
                        "contract": pin,
                        "captured_path": by_object[pin["object"]],
                        "bytes": len(body),
                        "scope": (
                            "Exact declared interpretation, not a claim of compatibility, "
                            "truth or latest-head applicability."
                        ),
                    }
                )
        return {"paths": paths, "selected": exposed}
