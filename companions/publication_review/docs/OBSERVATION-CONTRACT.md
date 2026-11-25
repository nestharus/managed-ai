# Publication observation producer

`publication_observer.py` is an independently usable standard-library Python producer. Its caller declares dependency meaning; this module measures the declared current inputs. Reviewer owns the exact assessor, joined review CLI/report and general operating guide. The shared API below is implemented and has been adopted by the captured consumer.

## Callable route

```python
from publication_observer import observe, observe_file
observations = observe(declaration, roots={"tree": "/absolute/current-tree"}, base_dir="/absolute/config-dir")
current = observations["current"]  # pass directly to exact assessor.assess(previous, current)
origins = observations["origins"]
# Or load a JSON declaration, resolving relative locations from its directory:
observations = observe_file("/absolute/declaration.json", roots={"tree": "/other/tree"})
```

`observe` does not change its declaration or roots. It returns a new JSON-compatible object with schema `publication-observations/1`, `current` and `origins`. `current` always has `requirements`, `sources`, `artifacts` and `selections` maps. The producer never rewrites a previous receipt; the reviewer must pass the original previous object to the exact assessor. Use ordinary artifact observation for both a consumer's own file and named intermediate files referenced by previous `accepted_inputs`.

## Declaration

```json
{
  "schema": "publication-declaration/1",
  "roots": {"tree": "./current-tree", "evidence": "./receipts"},
  "requirements": {
    "publication-duty": {"text": "Interpret the accepted current findings.\n"},
    "contract-file": {"root": "tree", "path": "requirements.txt"},
    "receiving-contract": {
      "receipt": {"root": "evidence", "path": "acceptance.json"},
      "field": ["basis", "requirements"]
    }
  },
  "sources": {
    "method": {"root": "tree", "path": "method.md"},
    "intentionally-unobserved": {"observe": false}
  },
  "artifacts": {
    "publication": {"root": "tree", "path": "publication.md"},
    "accepted-intermediate": {
      "root": "tree", "path": "intermediate.py",
      "acceptance": {
        "receipt": {"root": "evidence", "path": "acceptance.json"},
        "artifact_path": "intermediate.py"
      }
    }
  },
  "selections": {
    "publication-search": {
      "ledger": {"root": "evidence", "path": "source-receipts.json"},
      "receipt_id": "EXPLICIT_ACTUAL_SELECTION_CONTENT_ID"
    }
  }
}
```

All four input maps and `roots` may be omitted. IDs are nonempty strings. A declaration may include a descriptive `description`. Unexpected fields in observed specifications are rejected. Each category member is an object; `{ "observe": false }` explicitly leaves it unobserved. An undeclared member is also unobserved but has no origin entry. To obtain an observed absence, declare and actually check a file path; there is no declaration shortcut that asserts absence without a read.

A file binding is `{ "root": "named-root", "path": "relative/file" }` or `{ "path": "/absolute/file" }`. Without a root, a relative path resolves against `base_dir`; `observe_file` uses the declaration's directory. Named root paths may be absolute or relative to that same base. `roots=` overrides their locations without changing logical input IDs. Root-relative paths cannot contain `..`, be absolute, or resolve through a symlink outside the root. No directory is created. A missing/non-directory configured root produces unavailable observations, not an absent-file assertion.

Named-file identities are SHA-256 of the exact file bytes and contain no location. Moving equal trees changes paths in origins but leaves those logical IDs and hashes unchanged. Requirements are explicit content: `text` is encoded as UTF-8 without added newlines; requirement files must be valid UTF-8 and retain their exact original bytes (including final newline or BOM). The `receipt`/`field` form extracts an explicitly named string field from a supplied UTF-8 JSON file and hashes that string's UTF-8 bytes. A task label or expected digest is not a requirement body.

## Recorded receipts and acceptance assertions

Selection declarations choose an explicit content-addressed selection receipt from a supplied source-service ledger. This is the acquired helper's native ledger shape: `selections` and `inventories` maps. Each selection has `inventory_id`, `selector_id`, `query`, `query_interpretation` and `limit`; its inventory has `selector_id` and `selector` plus the complete recorded inventory fields. The utility verifies the selection and inventory keys against the SHA-256 of their complete canonical records, verifies matching policy IDs, and hashes the complete selector object. Canonical receipt JSON uses sorted keys, compact separators, `ensure_ascii=False`, finite values and UTF-8. Query identity hashes the object containing exactly `query`, `query_interpretation` and `limit`. The resulting basis uses `query_sha256`, `catalogue_sha256` (the inventory ID) and `policy_sha256`.

Origins retain the selected receipt IDs, complete query and selector, basis, ledger path/hash, and any recorded corpus root, claim and eligible counts. The historical inventory's location participates in its content identity when present. This intentional receipt location sensitivity does not change ordinary named-file identity. Reading the receipt neither discovers sources nor refreshes or establishes currentness of its catalogue. The caller chooses which recorded basis to compare. Contradictory selection/inventory/policy identity, missing explicit receipt ID, malformed JSON and duplicate JSON keys raise `ValueError`; they never become a validated matching observation.

An optional artifact `acceptance` assertion uses a supplied Workbench-style record with `accepted: true` and `artifacts_after[artifact_path].sha256` (and matching `bytes` when recorded). It verifies those against the actual named file bytes and includes the record's own observed hash in origins. A mismatch raises `ValueError`; to observe a changed file without asserting it matches a previous acceptance, omit this optional assertion. Plain current files are observations and have no acceptance claim. An unavailable acceptance record leaves the actual file observation intact and marks only the acceptance assertion unavailable. This does not authenticate the receiving authority or conclude that a current artifact fulfilled an obligation.

Recorded requirement, selection and acceptance specifications may each include `receipt_sha256` to pin the exact supplied receipt/ledger file bytes as an additional check. If the pin differs, observation raises `ValueError`. Without a pin, selection's internal content identities are still validated; an acceptance assertion binds to the exact receipt read but does not prove an external original receipt identity.

## Observation and acquisition account

For ordinary file inputs, a successful read yields a digest and `status: observed`; an actually checked absent target yields `null` and `status: absent`. Permission errors, non-file targets and unavailable roots omit the current map member with `status: unavailable` and diagnostics. Explicit skips omit the member with `status: unobserved`. A missing/unreadable ledger or requirement receipt does not observe the selected semantic input absent: it omits that input and records unavailable evidence. Invalid declarations or contradictory available receipts are explicit errors.

Origins include every declared member, complete resolved file locations and binding, actual byte counts/hashes and errors. `origins.reads` records one payload read attempt per resolved path; repeated bindings use that invocation's cached bytes. `origins.acquisition` counts file attempts, successful files and payload bytes, excluding root metadata checks and caller-supplied inline content. No cross-invocation freshness, atomic filesystem snapshot, semantic completeness or complete corpus observation is claimed.

## Producer CLI

```sh
python3 -B /absolute/product/publication_observer.py \
  --declaration /absolute/declaration.json \
  --root tree=/absolute/current-tree
```

It prints the complete observation JSON to stdout. Add `--output /absolute/new-observations.json` to write one explicitly requested new file; existing files are refused and parent directories must already exist. Command-line root overrides are resolved from the invocation directory; locations inside the JSON are resolved from its own directory. It works from an unrelated working directory. Importing the module runs no CLI. It never edits inputs, executes source content, runs source discovery/receiving commands or creates source roots.

## Source and joined-use scope

The exact acquired publication-use helper supplied the recorded selection validation and distinction between named bytes and recorded observation scope. The complete acquired assessor and DESIGN supplied the map boundary, missing-versus-null distinction, discovery fields and accepted-input relation. The new producer replaces occurrence-specific roots, IDs, hidden freeze files and registry assumptions with explicit caller declarations. The reviewer has adopted this exact boundary and implemented `publication_review.review`, `review_files`, and the combined `--declaration` CLI. The complete operating routes are documented in `README.md`. Private checks using actual captured peer code exercised observation followed by the exact assessor, preserved mixed reasons and independent current artifacts, retained the earlier accepted-intermediate basis, and kept obligation copies and inputs detached. These are development observations, not whole-product acceptance. Root's independent receiving and publication interpretation remain separate and pending at construction completion.
