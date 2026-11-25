# Interfaces and embedding

Install the wheel built from `engine/core/` as described in [Install or build](../core/CORE.md#install-or-build) or add `engine/` to Python's import path. The standalone launchers under `engine/core/cli/` perform that setup locally. Installed command names are `g60-collect`, `g60-work` and `g60-attend`. With either import setup, `python3 -B -m core.exchange` runs the exchange CLI.

The [architecture guide](architecture.md) explains the component relationships and the passage from a prepared context to a returned contribution, reuse and an explicit file effect.

## Implementation and host responsibilities

| Code | Implemented contribution | Supplied by the surrounding application |
|---|---|---|
| [collector.py](../core/exchange/transport/collector.py) | Admitted local command, actual byte streams, collected exit or visible uncertainty | Choice and permission to run the command; bounded foreground behavior; interpretation of the outcome |
| [Packet exchange operations](../core/exchange/) | Captured inputs, attributed returns, reusable proposed bodies, guarded single-file replacement | Source selection, actual recipient delivery, interpretation, current effect authority and any larger transaction |
| [attention.py](../core/attention.py) | Nominated questions, source/use-bound public accounts, selected feedback and historical access | Useful questions and alternatives, actual consideration, semantic adequacy and the next constructive choice |

Use only the relationship a current task needs. Existing files or a capable host may already supply it. The package does not need to become the controller of all application work, and research mechanisms described elsewhere are not installed by importing these modules.

## Collector

`core.exchange.transport.collector.Collector(root)` supplies `admit`, `resume` and `views`. Use an external writable runtime root. Direct callers must hold `exclusive(collector.root / "owner.lock")` around public operations, as the CLI does. Import `exclusive` from `core.storage`. The internal worker expects its recorded attempt token and is not another admission path.

`views(job_id="known-job")` inspects only that identity's directory. Missing or malformed targeted state returns `record-unresolved` with `requested_job_id`; it does not invent a stored origin identity. Without this keyword, `views` retains its all-job or named-consumer view. This selection does not dispatch or retry work.

`resume` dispatches all never-attempted admitted jobs in that root. It does not resume a killed process or replay an uncertain attempt. `engine/core/cli/collect_returns.py` checks that its root is outside the distributed package. The installed `g60-collect` command and direct `Collector` API do not perform that source-tree placement check; their caller must choose an external root. This is a storage-placement obligation, not a filesystem isolation boundary.

`completed` means an exit was collected; inspect the return code and actual output. See the [collector contract](contract.md).

## Continuation exchange

`core.exchange.Exchange(root)` supplies `initialize`, `prepare`, `context`, `dispatch`, `receive`, `recover_transport`, `close_transport`, `list`, `inspect`, `publish` and `reconcile_publication`. The CLI is the simplest supported integration surface. `initialize` takes its own owner lock. After initialization, direct callers serialize public operations with `exclusive(exchange.root / "owner.lock")` (or `shared` for read-only inspection). Exchange initialization rejects roots or workspaces inside the distributed package and requires the two locations to be disjoint.

`close_transport(packet, account)` records a verified launch failure or oversized completed reply as a terminal observation without a received contribution. `reconcile_publication(packet, target, expected_sha256, account)` acknowledges current base- or proposal-matching bytes while preserving the original publication intent. `expected_sha256` accepts a lower-case hexadecimal digest or `None` for an absent target. The CLI accepts the literal `absent`. Both operations preserve original evidence and return the original record on an exact repeat. The [exchange contract](exchange.md) gives their receiving and authority conditions.

`context(packet)` returns the validated original text with packet, consumer, request/context hashes and byte count. `g60-work context PACKET --text` renders that exact text. Current workspace loss or an unresolved publication does not block this historical read; immutable corruption does. The operation neither includes the context automatically in another packet nor grants its original authority to a new use.

`receive` verifies that the dispatch names this packet and its frozen request, then inspects that exact job. A mismatched dispatch is refused before carrier access; unrelated job history is not reread on every poll. An unresolved target record remains visible as a carrier outcome and does not become a received return.

`recover_transport(packet)` returns the complete captured Collector view for an
accepted local-command receipt and `None` for a caller-import receipt. Use
`g60-work recover-transport PACKET` or the native
`system.py --root RUNTIME recover-transport TURN` entrance for JSON output.
New receipts retain raw material in Exchange objects and carry compact metadata.
Recovery validates all body identities and lengths and restores the original
material fields, including base64 and the exact UTF-8 observation. It also reads
old inline receipts. The saved packet and objects support recovery after carrier
removal and current-source changes. The operation reads captured evidence and
preserves the original delivery's effect-authority scope.

`prepare(..., artifacts=["PACKET:TARGET"])` selects exact recorded proposal bodies without publishing or executing them. A later packet does not inherit the original target's effect authority. The selected return and content are validated. The [exchange contract](exchange.md) specifies source changes, interrupted publication and single-file effects.

## Attention companion

`core.attention.Attention(Exchange(root))` supplies `track`, `feedback`, `record`, `inspect`, `inspect_receipt` and `object`. Initialize the exchange first, and serialize direct calls with the same owner lock used by the CLI. The [attention contract](attention.md) defines specification, receipt, selected-view, object and delivery limits.

The returned structures describe captured public work. They do not expose private thought, infer all missing relationships, decide an item's importance, or prove that a bound artifact performs its claimed contribution. A host may deliver the returned context to its actual model loop; this package does not install that loop.

## Selected project-source reader

The optional standalone [selected-source reader](../../patterns/selected-source/README.md) supplies source observation without importing the deployment or engine, creating a context, or mutating owner state. It is separate from the installed core commands. Choose the existing knowledge library's `show` when a known logical body and catalogue pin suffice; choose this reader when the active owner-selection relationship is needed as well.

Its Python interface is `acquire(deployment, expected_selection, manifest_sha256, sources, *, max_bytes=8388608, max_metadata_bytes=8388608)`. Pass a deployment `Path` and a list of declared project paths. Both expected selection ID and manifest digest are required. `render(value, max_output_bytes=12582912)` returns the complete bounded JSON rendering; preserve that rendering check before emission to obtain the CLI's no-partial-output behavior. The guide gives the matching CLI, supported formats and all limits.

Acquisition checks the ordinary deployment/runtime bindings and stable entrance bytes, the active descriptor against its retained descriptor, and the exact manifest bytes against the caller's expected digest. The requested files must match declared bytes, hashes and modes; their declared directory ancestors are also checked. The reader uses the existing cooperative lock order and returns all selected bodies from that observation with an explicit unverified remainder. It does not verify unselected file contents, complete tree membership or engine/core bodies. Existing deployment `source`, `verify` and `system` retain their full checks.

The active project's text is not a complete current-owner view. A later correction, current goal or owed effect may live elsewhere and needs its own relevant observation. The reader neither applies that correction nor changes which engine will execute. A later source selection requires a new explicitly pinned observation even if the selected bytes happen to be identical.

## Discovery source acquisition

The agent-system discovery reader retains its separate entry, relation, source-byte and result limits. `max_bytes` admits declared body sizes or the observed record size. Its project, record and technique providers now bound actual acquisition with size checks and chunks of at most 64 KiB; growth can consume one extra sentinel byte before the query aborts. A record's parsed value and retained complete raw bytes come from the same read and pass the existing identity/digest validation. Valid candidate values, matching, ordering and budget admission remain unchanged.

The discovery source-byte budget does not include metadata enumeration, the enclosing deployment's complete-tree verification, interpreter work or peak allocation. Other provider calls retain their existing defaults unless they supply an explicit byte limit. These are properties of the published source: a continuing deployment uses its actual selected engine until a separate accepted transition changes it.

## Common boundary

All interfaces use standard-library Python on POSIX and native Windows. No model provider, credential acquisition, network service, agent runtime or native session-compaction adapter is implemented. Process boundaries and separate packets do not create independent semantic evidence. Current authority and source adequacy remain caller responsibilities.

Retention and recovery are also host work. An intact old return may support a new use without repeating its original command or publication. Preserve the request, bound objects and records needed by that use; deleting a content object can break several references. There is no object garbage collector or automatic reconstruction service. Choose retention against the actual recovery routes and occurrence-specific duties, not just the age of a packet.

The [tests](../../tests/engine/core/) exercise source capture, actual subprocess outcomes, return reuse, publication uncertainty, attention scope and standalone entry points. They establish behavior under the stated local contract. They do not compare model quality, full-use cost or autonomous discovery.

The [quickstart](quickstart.md) is a complete caller-authored local example. It demonstrates transport and record behavior, not a model invocation.
