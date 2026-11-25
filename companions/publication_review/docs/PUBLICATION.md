# Portable publication observation and review

Observe explicitly declared current inputs and compare them with a previous publication basis using the exact accepted continuation assessor. The result identifies which artifacts need reconsideration, while retaining matching independent artifacts and every previous open obligation.

Keep `publication_review.py`, `publication_observer.py`, and `continuation_assessor.py` together. Only Python's standard library is required. `OBSERVATION-CONTRACT.md` is the complete declaration and recorded-receipt reference; `PUBLICATION-CONTRACT.md` summarizes the joined callable boundary. No research archive, historical workspace, service, credentials, or study-specific path is needed for normal use.

## Run from any working directory

```sh
python3 -B /absolute/product/publication_review.py \
  --previous /absolute/previous.json \
  --declaration /absolute/declaration.json \
  --root tree=/absolute/current-tree \
  --output-dir /absolute/existing-parent/new-review
```

The command reads the declaration, measures actual named bytes, validates any explicitly selected recorded receipt basis, and directly calls the unchanged assessor. Root overrides change locations only. Relative locations in the declaration resolve from its directory; relative command-line root overrides resolve from the invocation directory. Omit `--output-dir` to receive the complete report on stdout without writing output files.

An explicitly requested output directory must be fresh and its parent must already exist. It contains `previous.json`, `current.json`, `observed-origins.json`, `observation.json`, `assessment.json`, and `report.json`. `assessment.json` is the exact assessor result. `report.json` preserves that result, the complete observation and its origins, and one explanation per artifact in original order. Output failure returns nonzero; an already created partial output is retained and must not be reused as a successful occurrence. Input/receipt errors produce a diagnostic on stderr and exit 2. A successful invocation exits 0; this describes the calculation, not publication acceptance.

Additional independently useful routes:

```sh
# Observe only; stdout is {schema, current, origins}.
python3 -B /absolute/product/publication_observer.py \
  --declaration /absolute/declaration.json --root tree=/absolute/current-tree

# Consume an already produced complete observation.
python3 -B /absolute/product/publication_review.py \
  --previous /absolute/previous.json --observation /absolute/observation.json

# Consume explicit assessor maps and optional origins.
python3 -B /absolute/product/publication_review.py \
  --previous /absolute/previous.json --current /absolute/current.json \
  --origins /absolute/observed-origins.json
```

The last two routes accept caller-supplied observations; they do not independently validate their provenance. The joined `--declaration` route performs the producer's documented checks.

## Declare real inputs

A small declaration is:

```json
{
  "schema": "publication-declaration/1",
  "roots": {"tree": "./current-tree"},
  "requirements": {
    "publication-duty": {"text": "Review the publication when its declared source changes.\n"}
  },
  "sources": {
    "findings": {"root": "tree", "path": "findings.md"},
    "deferred-source": {"observe": false}
  },
  "artifacts": {
    "publication": {"root": "tree", "path": "publication.md"},
    "independent-method": {"root": "tree", "path": "method.md"}
  }
}
```

Logical IDs such as `findings` remain stable when trees move. File identities are SHA-256 of actual bytes, without their locations; locations and read diagnostics belong to origins. Inline requirements hash the exact supplied string encoded as UTF-8, without an added newline. Requirement files must be valid UTF-8 and are hashed as their exact original bytes. A requirement may instead name a specific text field in a supplied receipt; see the producer reference. An invented task label is not a requirement body.

The previous document has an ordered `artifacts` array and an `obligations` array. Each artifact receipt requires `id`, its prior `artifact_sha256`, and its prior `requirements` and `sources` digest maps. Optional `scope: "discovery"`, `selections`, and `accepted_inputs` retain their acquired meanings. The consumer passes the supplied previous object through unchanged; it never rebuilds the previous basis from the latest observations.

For a new declared baseline, a caller may explicitly observe the initial files and construct a receipt from those measured identities. For example, after loading the declaration above:

```python
from publication_observer import observe

initial = observe(declaration, roots={"tree": "/absolute/initial-tree"})["current"]
previous = {
    "artifacts": [
        {
            "id": "publication",
            "artifact_sha256": initial["artifacts"]["publication"],
            "requirements": {"publication-duty": initial["requirements"]["publication-duty"]},
            "sources": {"findings": initial["sources"]["findings"]}
        },
        {
            "id": "independent-method",
            "artifact_sha256": initial["artifacts"]["independent-method"],
            "requirements": {}, "sources": {}
        }
    ],
    "obligations": [
        {"id": "owner-review", "owner": "publication-owner", "status": "open",
         "occurrence": {"id": "declared-baseline-1", "details": ["Review actual findings"]}}
    ]
}
```

Persist that baseline when it is declared. Constructing a present comparison scope does not recover unknown historical authoring dependencies. The new declaration does not itself certify acceptance of its artifacts or fulfillment of an obligation.

## Importable use

With the product directory on Python's module path, the entire route is:

```python
from publication_review import review, review_files, assess_documents

report = review(previous, declaration, roots={"tree": "/absolute/current-tree"},
                base_dir="/absolute/config-directory")
exact_result = report["assessment"]

# Or resolve relative input bindings from the JSON declaration's own directory.
report = review_files("/absolute/previous.json", "/absolute/declaration.json",
                      roots={"tree": "/absolute/current-tree"})

# If actual observation maps already exist, invoke only the exact assessor.
exact_result = assess_documents(previous, current)
```

From an unrelated directory, use `PYTHONPATH=/absolute/product python3 -B your_script.py`, or load `publication_review.py` with `importlib.util.spec_from_file_location`. Its sibling modules are located relative to its own file, not the working directory. Importing runs no CLI. Methods perform no writes; explicit CLI outputs are the only utility writes. `-B` also suppresses Python's ordinary import caches.

`review_observations(previous, observation)` accepts the producer package directly. Inputs remain unmodified. Returned observations are detached copies; the exact assessor supplies detached copies of every previous `status == "open"` obligation, preserving order, nested fields, owner, and occurrence identity.

## Interpret the result

| Result | What it establishes under the declared basis |
| --- | --- |
| `current` | Every declared comparison has a matching supplied observation. |
| `recheck` | At least one declared identity changed or was explicitly observed absent. |
| `unknown` | No known change exists, but a necessary observation or discovery basis is unavailable. |

Known change takes precedence over unknown, while all reasons remain present. A checked absent ordinary file becomes JSON `null`. Explicit skips, undeclared inputs, inaccessible files, non-file targets and unavailable configured roots omit the observation instead. Origins distinguish these circumstances. A missing receipt ledger is unavailable evidence for its selected basis; it does not establish that a semantic selection is absent. Missing source roots are never created.

The result preserves artifact order, exact statuses, and every reason from the accepted assessor. Independent artifacts keep their own declared basis: a change elsewhere does not add undeclared dependencies. `recheck` nominates owner work, without establishing that a conclusion is false or authorizing a rewrite. `current` does not complete a separate duty. Open obligations are retained regardless of artifact status.

## Discovery and accepted intermediates

A discovery receipt declares the exact selection query, catalogue and policy digests under its selection ID. The current declaration must explicitly choose a recorded source-service `receipt_id` from a supplied ledger. The producer checks the full selection and inventory content identities and corresponding selector policy, then measures the acquired three-field basis. It never guesses a receipt, searches the corpus, or refreshes a catalogue. A changed recorded catalogue nominates review; it is not a semantic completeness test.

Ordinary named-file hashes are location independent. A native recorded inventory may include its original `corpus_root`; this location is part of that recorded content-addressed identity. The producer preserves and explains this intentional recorded-receipt sensitivity instead of silently changing its meaning after relocation.

If a previous receipt declares `accepted_inputs: {"intermediate": "EARLIER_ACCEPTED_SHA256"}`, declare a current artifact named `intermediate` to observe its actual bytes. The assessor compares the earlier accepted digest to the current file digest. A changed intermediate remains changed even when the publication and terminal sources are unchanged. Do not replace the previous accepted digest with the new one.

An optional current artifact `acceptance` declaration separately asserts that the current file matches a specifically supplied `accepted: true` receipt and its named `artifacts_after` entry. The producer validates actual digest and recorded byte count, retains the receipt identity, and rejects contradictions. To observe a changed file without claiming it matches an old acceptance, omit that optional assertion. If its acceptance record is unavailable, the current byte observation remains usable and origins mark the assertion unavailable. An arbitrary current file has no automatic acceptance claim. A `receipt_sha256` pin can additionally require exact supplied receipt-file identity.

Available contradictory or tampered recorded identities raise explicit errors. Recorded evidence is not authenticated receiving authority, fresh discovery, or proof that a duty was fulfilled.

## Exact acquired component and limits

`continuation_assessor.py` is distributed unchanged: **7,100 bytes**, SHA-256 **`3109ffeb01032d2f4dbc3afa6df2b05d9bb8a1f68f51561c86d2a7efbd6e1f67`**. The consumer verifies those bytes before loading and directly invokes that module's `assess(previous, current)`. Optional `--assessor`/`assessor_path` selects another explicit path with the same exact identity. No rewritten decision algorithm substitutes for it.

The complete supplied assessor, its DESIGN/operating contract, and the acquired publication observation helper informed this construction. The helper's recorded selection validation is retained, while its occurrence-specific roots, registry, hidden freeze files and mandatory historical paths are replaced by caller declarations. The current canonical method supported the direct composition and preservation of independent continuing duties. These source premises do not certify a new broad efficacy or economic claim.

The producer reads only declared inputs. It does not infer complete dependencies or enumerate a corpus. Each resolved payload is read at most once per invocation; acquisition counts exclude root metadata checks and caller-supplied inline text. The collection is not an atomic filesystem snapshot. Input bodies are never executed; no source-discovery or receiving command is invoked. The utility neither edits publications nor changes requirement bodies, previous receipts, the assessor, or obligation records.

Local construction checks provide development feedback. Structural peer capture, exact calculation, independent receiving, and the publication owner's semantic decision remain separate. The two original-history duties in this construction remain exact and open independently of finite product completion.
