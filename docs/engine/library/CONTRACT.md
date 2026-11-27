# Knowledge acquisition and product contract

From the Managed AI Information package root, `src/managed_ai_information/library/cli.py --store STORE discover --query TEXT` searches the selected store catalogue and `show SOURCE_ID` reads a selected body. The library operations below use `src/managed_ai_information/library/cli.py --library LIBRARY` and require that library’s catalogue and objects.

## Participation and source identity

This is a small information component for an AI system whose capable model or human already exists. It supplies complete source exposure, authored module views, bounded lexical discovery and data-only interchange. It does not invoke a model, infer semantic completeness, select a current deployment, perform research by itself or grant effects. A capable host can use the bodies directly, use the separately supplied agent-system or compose another arrangement.

Every body has a stable logical `id`, complete UTF-8 `object`, SHA-256, byte count, title, navigation summary, current source owner and original `link_base`. The object's bytes equal the complete nominated source's bytes. A summary nominates a route; it is never substituted for the body. Objects are content-addressed `.txt` files and are never executed. Links within an unchanged copied body retain their original interpretation: use its `references` inventory to request another logical identity from the library. An absent target is an outside recovery route, not a silently resolved neighboring file.

`library_sha256` pins the exact catalogue bytes. Body pins also bind the body's complete SHA-256. Module pins bind canonical JSON of their entry, including purpose, members, prerequisites, parent roles and external participation. An edition name is a human label, not a version comparison. Equal body bytes can acquire a changed role or meaning in a new module or receiving use. Interpretive change therefore needs its own recorded contribution even where content hashes remain equal.

Schema identity, content identity, module interpretation and current adoption are distinct. The adapter accepts only `knowledge-library/1` and `knowledge-selection/1`; unknown formats are refused. A future schema migration must explicitly construct a successor catalogue, preserve or account for logical IDs and complete bodies, nominate changed interpretation and retain the old edition. There is no automatic “latest” selection or implicit migration during import.

<a id="select-complete-understanding"></a>
## Select a sufficient representation and acquire it exactly

Select the semantic unit the receiving question needs. That unit can be a full argument, a compact question-relative view, a qualified local answer or an executable contract. A derived view has its own body identity, purpose, relevant validity conditions and route to further participation. Completeness of the selected body is an integrity promise at this unit; it does not require total source transmission or imply complete understanding. The catalogue's navigation summary remains only a discovery aid.

Ordinary references and ancestry do not automatically become context prerequisites. Acquire additional source distinctions when the selected use consumes them. If a module declares a large closure that is unsuitable for the question, choose a sufficient existing body or construct a qualified view under its own identity. Do not weaken that module's declared promise silently. Whole judgment can compose several such receiving contributions while detailed local inquiry remains available.

Run `show ID` to acquire a complete body and its pin. `show ID --text` returns only its exact complete text. Run `modules` to inspect the full available module entries and their pins. A selection is JSON with exactly these fields:

```json
{
  "format": "knowledge-selection/1",
  "purpose": "Explain which changed shared source affects the current receiving composition.",
  "bodies": [],
  "modules": [
    {
      "library_sha256": "COPY_THE_ACTUAL_64_HEX_LIBRARY_DIGEST",
      "id": "shared-work",
      "sha256": "COPY_THE_ACTUAL_64_HEX_MODULE_DIGEST"
    }
  ]
}
```

These explanatory placeholders are deliberately not valid pins. Copy actual returned values. Individual `bodies` use the same three fields. `context` resolves selected modules, their explicitly required modules and all required complete member bodies, then includes their full entries and text. A stale pin, unavailable prerequisite or oversized selection refuses the operation; it never substitutes partial text. Supply `--library PATH` before the subcommand, repeatedly when composing from multiple libraries. Selecting two libraries with the same logical body/module identity and incompatible entries is allowed in a comparison context, but product export requires an explicit version choice and refuses their silent merge.

The default context body budget is 8 MiB; `--max-bytes` changes it up to 32 MiB. The complete rendered JSON has a separate 36 MiB cap because quoting and metadata can enlarge it. Complete exposure is not evidence the recipient has read or used the sources. Retain an actual reading/use account when a later interpretation consumes that difference.

These limits bound one adapter operation. A smaller sufficient contribution can be prepared elsewhere and selected here without carrying its full ancestry. Its semantic adequacy is supplied by the actual producing/receiving relationship, not inferred from its small size or successful package validation. Known duties already accepted by another adapter retain that adapter's contract.

## Discovery and discoverable views

`discover --query TEXT` defaults to `--backend bm25`: whole Unicode tokens, a disclosed English function-word policy, document-frequency and length-aware lexical ranking, and stable identity ties. Meaningful short tokens are retained. Double-quoted spans request ordered token phrases and preserve their function words. Retained terms and phrase units are combined by OR: each candidate reports the units it actually matched. Scores from different backends, query scopes or corpora are not a shared calibrated scale. An ordinary function-word-only query yields no lexical candidates; this describes the selected query policy, not the absence of useful knowledge. Exact logical-ID nomination is preferred when that identity lies in the examined scope; `show ID` remains the direct acquisition route independent of discovery limits.

`--backend fts5` selects the pure-stdlib positional Porter-stemmed BM25 inverted index with token phrases (zero SQLite dependency). Ordinary query text is tokenized rather than executed as raw query syntax. Stemming can improve one query and worsen another; it is not semantic equivalence. `--backend literal` retains the earlier whitespace/subword matcher for explicit diagnostic comparison, and `backend="membus"` delegates to a configured `MembusV2Backend` for Tantivy BM25 search. The result identifies the backend, effective terms/phrases, ignored terms and scoring policy. Scores are ordering signals within that query and scope, not calibrated relevance probabilities or evidence that a body supports a claim.

The default examines at most 512 complete bodies and 8 MiB, and returns at most 20 bodies and 20 modules. `--scan-limit`, `--max-bytes` and `--limit` bound different work. An oversized body is skipped rather than truncated; remaining bodies may still be examined. The result records examination counts, exact omitted body IDs and truncation of shown results. Ranking happens within that examined scope. A better ranker cannot recover a body excluded by an earlier scan or byte limit. No result proves that an omitted region has no useful source. Known identities can be read directly regardless of discovery ordering.

Ordinary discovery keeps an in-memory derived object on the `Library` instance. Each call still reads and verifies every examined complete body before using prepared ranking data; a reused index avoids retaining unused full-text materialization. Reuse depends on the exact catalogue, selected scope and analyzer configuration. One-shot ordinary CLI calls pay preparation anew. A `Library` retains the catalogue snapshot loaded at construction; construct a new instance to select a newly published catalogue or metadata edition. Calls on one instance must be serialized in its owning thread; independent workers use their own instances. No implicit persistent cache is created.

A module has `id`, `title`, `purpose`, `members`, `requires`, `parents` and `external_participation`. A member names a required complete body. `requires` names another required module; cycles are traversed once. `parents` are `{id, role}` explanations of ancestry, contribution or relationship; they confer neither membership nor authority. Parents may be outside the library. External participation states a receiver, capability or owner condition and why it is outside the data package.

Membership is many-to-many. It nominates a possible receiving relationship, not exclusive ownership, a fixed domain taxonomy or proof every consumer is affected by every change. Maintenance ownership, discovery grouping, exported closure and current authority remain separate. Revise or construct a useful module in the authoring source map and publish a new edition. The new purpose and membership should follow an actual prediction or receiving return. There is no automatic optimum partition or claim the initial authored views were autonomously discovered.

## Reuse an explicit prepared index

For repeated nomination from one selected edition, `prepare-index --output PATH` verifies the selected complete bodies and builds a fresh immutable contentless compressed inverted index (`MAI-LEX-IDX/1`). Its ordinary defaults are 512 bodies and 8 MiB; `--scan-limit` and `--max-bytes` explicitly select another bounded scope. The JSON build receipt gives the exact index digest, size, indexed IDs, omissions and complete basis. Keep the receipt from the controlled build. The index stores terms, positions and lengths; it does not keep a second copy of the complete source prose and cannot replace canonical source custody.

Use `discover --backend fts5 --index PATH --index-sha256 DIGEST --query TEXT` with that actual receipt's digest and the same scan/byte settings. The reader verifies the bounded derivative bytes before querying their exact image, then requires the selected catalogue, body/module ordering, query policy, tokenizer, weights, engine/Unicode versions and declared scope to agree. Wrong digest, changed basis or legacy binary SQLite files are refused; rebuild deliberately or use ordinary discovery. There is no silent change of backend, source edition or claimed verification scope. All backends run with pure standard-library Python (zero SQLite dependency).

Indexed discovery performs **no canonical-body reads on that call**. `preparation.index_verified` records whether derivative verification actually occurred; a query filtered to no searchable units can return empty without opening the index. Coverage distinguishes indexed representations, their represented body bytes, derivative read bytes and zero freshly verified canonical bodies. Candidate pins still identify exact complete sources. `show ID`, `context`, export and `verify` retain their source checks. A body changed outside its selected edition can remain nominated by the old index and then fail exact acquisition; this is the declared snapshot relationship, not a fresh all-source integrity audit. A new catalogue must be selected with a new `Library` and matching preparation.

The expected digest identifies the chosen derivative under the caller's source-trust arrangement. A digest asserted by an arbitrary producer, matching metadata labels, or structural row counts alone do not prove that arbitrary postings represent the source. The provided builder's complete verified construction and the actual retained receipt supply that correspondence. Lexical rank equality for the selected analyzer and source scope does not establish semantic relevance or useful recall.

Preparation must target a nonexistent ordinary path outside the library, with an existing caller-owned parent. It stages and fsyncs the complete file, then publishes using a non-replacing hard link and fsyncs the parent; the cooperating local filesystem must support those operations. Existing outputs are refused. Temporary staged files are cleaned on failure; a failure after publication can leave the complete destination, whose actual bytes must be inspected before retrying. The maximum index image is 40 MiB. Loading verifies the complete image, so acquisition remains linear in its bytes, with full selected-metadata comparison, deserializing into a private in-memory inverted index without creating temporary files on disk. Include that memory in the workload comparison.

The index is disposable caller-owned state and is excluded from the immutable delivered product. Rebuild after a changed source/analyzer basis, retain a deliberately historical one when that is the actual use, or remove an unneeded index. No automatic eviction, incremental update, embedding search or statistical audit is claimed. The logical limits in this component remain in force.

## Ask several questions of one prepared image

`discover-batch --queries questions.json --index PATH --index-sha256 DIGEST` nominates several already available questions from one selected prepared image. `questions.json` is a UTF-8 JSON array of 1–32 ordinary query strings, each nonblank and at most 2,048 UTF-8 bytes. The file and the normalized indented array each have a 128 KiB bound. Query interpretation, phrase handling, exact identities, ranking, module relationships and omitted scope use the same implementation as ordinary discovery. Duplicate questions retain their separate positions. This command accepts exactly one selected library and the FTS5 prepared backend; it does not silently switch analyzer or fall back.

The public Python form is `Library.discover_batch(queries, index=PATH, index_sha256=DIGEST, limit=20, scan_limit=512, max_bytes=8388608, max_result_bytes=8388608)`. `limit`, `scan_limit` and `max_bytes` have the existing per-question meanings and must match the preparation's selected scope where applicable. The API validates every question before opening the image. A malformed query, digest or bounds value refuses the batch.

If any question has searchable units or an exact known identity, the call reads, checks and opens the selected index image once. All such questions share its exact catalogue, analyzer and scope basis. A changed input file after this acquisition does not change the private selected image. The image closes on success, a query failure or a result-bound refusal; another batch reacquires and rechecks its selected image. This is an explicit one-call snapshot, with no cache retained across batches and no new long-lived thread-sharing contract. Ordinary `discover` continues to verify its image on every call.

The result format is `knowledge-discovery-batch/1`. Its `results` array contains ordinary `knowledge-discovery/1` responses in input order. Their `preparation` accounts identify the shared image and report zero additional per-question image reads. The enclosing `preparation` alone accounts for the actual image bytes and whether one image was opened and verified. `index_verified` on a searchable question means it used that verified shared image; it does not mean another read occurred. No canonical bodies are read by batch nomination. `show` remains the exact source acquisition operation.

A question filtered to no units and naming no exact identity has the same empty result and omitted-scope account as ordinary discovery; its preparation reports no required image verification. If every question has that property, the batch opens no image, even if its supplied path is absent, and reports `index_verified: false` and zero image bytes. The expected digest must still be syntactically valid. A truly blank query is invalid rather than a filtered query. An all-filtered batch can therefore complete without opening the prepared index image, just as an ordinary filtered prepared query can.

The complete returned JSON envelope, including all per-question omitted identities and metadata, is bounded at 8 MiB. `--max-result-bytes` or `max_result_bytes` can select a smaller positive bound. The check counts the actual indented UTF-8 representation with its final newline, including nested indentation; it does not count only candidate text. Construction checks each additional result before retaining it and does not return a partial result as a successful whole batch. The CLI emits no partial success output on refusal. This bounds the delivered envelope, not every intermediate allocation, database operation or latency; the existing catalogue, source, image and query bounds still apply.

These finite limits define one local operation. Separate later batches, a sufficient direct source or a deliberately managed service remain alternatives. Forming a batch can add waiting, and it only reuses work where questions share the selected basis. Neither batch size nor successful acquisition establishes semantic recall, workload optimality or global system scale.

## Own a lifetime for separately arriving questions

`source_producer.Producer` supplies a serial, in-process lifetime over the native FTS5 reader. It accepts separately arriving discovery and complete-acquisition requests. From an installed G60 consumer release, a Python host can use the following interface.

```python
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, "agent-system")
from source_producer import Producer

with closing(Producer(Path("agent-system/library"))) as producer:
    current = producer.handle({"operation": "describe"})
    pin = current["library_sha256"]
    found = producer.handle({
        "operation": "discover",
        "query": "source historical current",
        "library_sha256": pin,
        "use": "current",
    })
    acquired = producer.handle({
        "operation": "show",
        "id": "research/patterns/qualifying-reuse.md",
        "library_sha256": pin,
        "use": "historical",
    })
```

The host owns the instance and its eventual `close()`. A known body can go directly to `show`. Successful responses carry `status` equal to `ok`, the selected `library_sha256`, and the native discovery or complete-body view under `result`. A refused request carries `status` equal to `refused` and its `error`. `describe` reports current identity, available fields, bounds and preparation guarantees.

Each `current` request freshly observes the catalogue and requires agreement with its supplied pin. A `historical` request selects a catalogue already acquired during this instance. Every successful `show` freshly validates canonical length, SHA-256 and UTF-8 content under the selected catalogue.

The first searchable request for a corpus uses ordinary native discovery. Later requests over that same catalogue and ordered corpus query the retained native indexes. Their coverage reports prepared representations and zero freshly verified canonical bodies. A changed corpus is freshly acquired and replaces that catalogue's earlier indexes. Numeric bounds that select the same corpus can share the representation while retaining their own reported limits and omissions.

Each acquired catalogue has at most one retained index corpus. All acquired catalogues remain until closure. Canonical availability is required for a new corpus and for every complete acquisition. A retained nomination can still be used when a later acquisition refuses. Closing the instance releases its historical catalogues and indexes. A fresh instance reconstructs the currently published source. Continued historical service after closure requires another owned source arrangement.

This wrapper uses the issued native cache layout and result assembler. Changes to those internals require compatibility checks. The host supplies serial invocation and a cooperative source directory. Current authority is scoped to the catalogue observation within each request. Choose the lifetime and route from the actual receiving guarantees and measured resource vector. The research archive retains the Study 77 acceptance and actual publication occurrence.

## Export and import

`export` produces a fresh ZIP with an exact `catalogue.json` and content-addressed complete objects. The selected body's metadata and source pins remain inspectable; provenance retains the selection, participating library identities and unselected reference routes. Declared required bodies/modules must be present. An ordinary Markdown link, URL or parent relationship is not itself a declared prerequisite. The receiver judges whether the declared closure and external participation actually supply its promised use. A missing prerequisite can therefore be a semantic defect even when package verification passes; repair the responsible module or product promise.

`inspect` verifies the whole package without importing it. It rejects unsupported schema, duplicate JSON keys, duplicate or excess archive members, undeclared members, missing bodies, nonregular entries, encryption, digest/length mismatches and bounds violations. It never extracts supplied paths. Entry names must exactly equal the catalogue and declared content objects. Metadata, including a source path or outside URL, is data and is not followed by import.

`import` first completes those checks, then installs only data into a fresh directory named by the complete archive SHA-256 under an existing caller-owned store. A short cooperative POSIX lock, staged files, fsync and atomic rename protect the import commit. Repeating an already committed import verifies exact content and membership and returns `already-present-verified`. A conflicting destination is refused. Crashed unpublished staging directories are not selected libraries. Do not infer content authenticity from a digest alone: the caller still selects the source it trusts for the stated use.

Imported data conveys no source ownership, credentials, capability, active engine selection or effect permission. Code excerpts remain text. Explicitly constructing a local artifact from the text is a separate owner action. In the collective-work adapter, retain the original body/library pins alongside that artifact; use its captured current basis and scoped owner policy for later effects. A module change can nominate a consequential use through a source-pinned offer. The receiving owner decides the relationship and any needed current-use treatment.

## Bounds and host assumptions

The adapter uses Python's standard library. Limits are 4 MiB per catalogue, 2 MiB per body, 2,048 bodies, 256 modules, 32 MiB of selected complete body bytes and 40 MiB of ZIP bytes. Each library is checked independently; a multi-library context/export also applies the selection total. Source trees and stores must use ordinary files/directories without symlink components. Import commit uses a cooperating local POSIX filesystem. This is not an authenticated multi-tenant service or hostile-filesystem sandbox; replace that boundary if the receiving deployment requires it.

Commands return JSON; refused operations exit 2. `publish.py audit` exits 1 when selected current source bytes or the source-map definition differ. Library verification establishes exact bodies and declared structure; it does not establish that a current source edition, independently changed interpretation or receiving duty still agrees.

## Verify the component and a receiving use

```bash
python3 -B knowledge.py verify
python3 -B -m unittest discover -s ../tests/library -v
```

The tests exercise package integrity, complete closure, stale pins, collisions, bounded selection, malformed imports and idempotent admission. A meaningful system check also lets a capable recipient discover, read and use an exported module for a declared contribution, then receives and interprets that return. Mechanical package checks cannot supply that latter evidence.
