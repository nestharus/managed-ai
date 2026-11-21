# Read complete selected project sources with a stated assurance scope

`read_selected.py` is an optional, standalone, standard-library POSIX reader for a specific receiving need: **obtain complete UTF-8 project sources and establish that their exact bytes belong to the caller's expected active deployment selection**. It accepts several sources for one observation. It imports no deployment or engine code, creates no files, and performs no system operation.

This is a known qualified-acquisition mechanism made reusable. Use it when the active owner selection matters to the receiving decision. The existing knowledge library's `show` is already sufficient for a known logical body under its catalogue pin when that owner relationship is irrelevant. A manually checked deployment status, pinned manifest and bounded source read can establish the same narrower relationship. Full deployment `source`, `verify` and `system` retain their existing stronger complete-tree checks and execution conditions.

## Invocation and returned evidence

Obtain the current selection and accepted manifest pin through the owner's existing reviewed route, then pass both explicitly. The following values and paths are illustrative:

```sh
python3 -B runtime/patterns/selected-source/read_selected.py \
  --deployment /absolute/path/to/deployment \
  --expected-selection EXPECTED_SELECTION_ID \
  --manifest-sha256 EXPECTED_64_CHARACTER_LOWERCASE_SHA256 \
  --source research/current/method.md \
  --source research/current/frontier.md
```

The caller must choose actual declared file paths. There is no directory expansion, substring selection, catalogue search, fallback selection, automatic new pin, engine invocation or state mutation. A changed selection is refused even if the newly selected body has identical content. The expected selection is an observation precondition, not a request to activate it.

Success writes one complete JSON object and a final newline to stdout. It contains:

- The deployment, runtime and workspace identities; complete active selection descriptor; exact manifest path, digest and byte count.
- Each complete source text in requested order, its declared relative path, byte count, SHA-256 and ordinary mode, and its actual absolute path.
- The checked project directory ancestors, actual successful metadata/entrance and selected-body byte counts, and the explicit assurance statement.

Refusal returns exit status `2`, a bounded JSON error on stderr, and **no stdout**. A failed read, stale expected selection, held lock or output limit never returns a partial source result. `--help` is ordinary CLI help. The Python API separates `acquire(...)`, which returns the checked data, from `render(value, max_output_bytes=...)`, which completes the output-bound check before emission. API users must preserve that rendering boundary if they want the CLI's no-partial-output contract.

The result is a receipt of this observation. Its pins and paths identify omitted metadata, including the complete manifest; they do not reconstruct that metadata or constitute a self-contained portable proof or recovery archive. Retain the separate exact metadata when a later receiving use needs it.

## What the observation establishes

The reader supports the current deployment format `agent-system-deployment/1`, controller `0.2.0`, runtime format `g60-agent-system/0.1` and declared core version `0.4.1`. Unsupported versions, extra or missing schema fields, duplicate JSON keys, ambiguous members and malformed declarations are refused. Such refusals require an explicitly reviewed future reader change; they do not propose a core migration.

It checks ordinary deployment and runtime ancestry, exact absolute deployment/runtime/workspace bindings, runtime metadata, and the stable `deploy.py` and `deployment.py` bytes against the deployment configuration. It checks that the active descriptor exactly equals its retained descriptor, that its selection and manifest digest match the caller's expectations, and that the digest covers **the same raw manifest buffer that is parsed**. Parsing and hashing do not independently reread the manifest.

Manifest file and directory declarations must be unique, canonical and structurally complete. Absolute members, aliases such as `a/../b` or `a//b`, backslashes, special permission bits and file/directory collisions are refused. For the requested project files, the reader checks actual ordinary file type, exact bytes, SHA-256, mode, and all declared project-directory ancestor modes. Visible symlinks anywhere along the observed paths are refused. It checks open file descriptors as well as the paths and rechecks observed identities before completing the acquisition.

The existing cooperative lock order is preserved: `deployment.lock`, then the runtime's `exchange/owner.lock`. Both locks must already exist as ordinary files. They are opened read-only and acquired exclusively without waiting; the reader never creates them. The locks remain held through all selected reads and final observation checks. This bounds one cooperating owner's observation window. It supplies neither perpetual currentness nor protection against an adversarial filesystem writer, and a supplied pin is not an authentication system.

## What it leaves unverified

The return includes an explicit unverified remainder. The reader does not walk or read unselected project files, verify complete tree membership, read or audit the engine/core bodies, compare an engine to a declared project subtree, audit historical selections, or execute anything. Parsing the manifest's declarations does not establish their corresponding unselected bytes.

Consequently, changing an unrelated file can leave this selected-source acquisition valid while the existing full deployment verification correctly refuses. This is an intentional difference in assurance, not equivalent verification with fewer reads. A caller who needs complete-tree assurance must use the existing operation for that guarantee.

**Active selected-project identity also does not mean every current owner fact or the newest working correction.** A later accepted correction can have a separate retained landmark while the immutable active project still contains the earlier source. Current goals, owed effects, applicability of a later correction and all other runtime facts require their own current receiving checks. Acquiring a method or frontier source can support a next-research decision about those sources; it does not settle those other facts, supply semantic acceptance or authorize an effect.

There is no claim here to lower the costs of record writes, current-use interpretation, context capture or arbitrary engine calls. One existing system context can already acquire several sources for a commissioned task, while producing actual context state. This reader is suitable when the receiving task calls for a source observation without that production.

## Bounds

All limits below are bytes, and source limits apply to the complete UTF-8 input before JSON escaping. An invalid or excessive requested limit is refused before acquisition.

| Quantity | Default | Supported maximum |
| --- | ---: | ---: |
| Requested unique sources | Caller supplies 1–64 | 64 |
| Each complete source | 2 MiB | 2 MiB |
| Aggregate selected source bytes (`--max-bytes`) | 8 MiB | 32 MiB |
| Aggregate metadata and entrance bytes (`--max-metadata-bytes`) | 8 MiB | 16 MiB |
| Complete rendered stdout (`--max-output-bytes`) | 12 MiB | 64 MiB |

The manifest itself is limited to 8 MiB, deployment configuration to 256 KiB, other individual JSON metadata files to 64 KiB, and each stable entrance to 2 MiB. The aggregate metadata budget includes both stable entrances and every JSON file read; there is no free unbounded metadata pass. The manifest therefore remains a cost proportional to its declarations even when unrelated archive bodies are not read.

Every read checks the current ordinary stat size before opening, checks the actual opened descriptor, and reads in chunks of at most 64 KiB. A static oversized or wrong-sized source is refused without reading its body. The reader admits the current exact size and allows one additional sentinel byte to detect growth. If a file grows after admission, acquisition aborts; **that aborting read may consume one byte beyond its admitted size and at most one byte beyond the aggregate budget**. Successful reads stay within both aggregate budgets. A successful byte account describes the bytes returned by this reader's file reads, not physical storage traffic, interpreter imports, latency or peak memory.

The complete result is rendered and its byte limit checked before writing stdout. Escaping can make JSON larger than its source bodies; an otherwise valid acquisition may therefore be refused by the separate output limit. This is an emitted-output bound, not a peak-allocation guarantee. The error payload is separately capped to a short JSON diagnostic.

## Concrete receiving decision and evidence

A useful receiving case is to obtain the selected canonical method together with a selected frontier or applicable pattern, read both complete texts, and decide what next research encounter they support. Retain the exact selection, manifest and body pins beside that explanation. Reopen applicability if the owner changes selection, a later correction matters, or the task now needs other current facts. Where the decision only needs the same known logical texts, the existing knowledge reader is the adequate simpler route.

The contribution's isolated fixture encounters, unchanged full-verifier comparison, measured actual-owner acquisition and receiving review belong in the containing research evidence. This source does not infer their success merely from its implementation or promote fixture results into general scalability claims.
