# Shared knowledge system

**Package scope.** This directory retains selected authoring sources and contracts. The prepared CLI and catalogue are in the separate [operating release — additional source](../../SOURCES.md#source-dbcc210d7f4962499db6). Publishing and rebuild commands below describe the original source workflow and can require its full source workspace.

In the v28 working source, publish the current knowledge catalogue into a new local destination with `python3 -B prototype/knowledge-system/publish.py --root . publish --edition local --output ../my-knowledge`. Then pass `--library ../my-knowledge` to the reader. The installed compact includes its selected published library.

An AI system joins a capable interpreter, useful information, operations and actual receiving work. A contribution can be an explanation, a design, a review, code or a new system. Predict what the contribution needs; discover or construct that understanding; project the effect forward and its necessary participation backward. Select a representation that supplies the needed distinctions, use it, and let the return change both the work and the means available for later work.

**Start here for agent understanding and continued research.** This expandable library supplies complete research explanations, operating knowledge and pattern bodies. Its Python adapter supplies bounded discovery, exact acquisition and data interchange. Your capable host supplies interpretation, further inquiry and authorized effects. The [working contract](CONTRACT.md) distinguishes those contributions; [research participation](RESEARCH.md) carries current questions and returned improvements through the same system.

For ordinary general-system adoption, `G60-system-release.zip` provides a smaller selection with direct readable files, complete pinned originals and an optional runtime. This full library retains wider current research and source discovery. A logical identity such as `research/current/method.md` names a body for `knowledge.py show`; it does not promise that the author's research directory exists beside a copied library. The release supplies local reading paths for its default uses.

From the prepared operating release, or a complete published library with its catalogue and object bodies, use Python 3.10 or later:

```bash
python3 -B knowledge.py discover --query "shared work changed interpretation"
python3 -B knowledge.py show research/current/method.md --text
python3 -B knowledge.py modules
```

Discovery ranks whole-token matches with BM25 and reports its query policy, examined sources and omitted scope. Short meaningful tokens remain searchable; common English function words in ordinary queries do not become incidental substring matches. Use `--backend fts5` for optional SQLite FTS5 English stemming and quoted token phrases, or `--backend literal` to reproduce the earlier substring matcher. FTS5 must be available in the host's SQLite build. Follow a returned identity with `show`; ranking nominates routes and does not answer the question. A known useful source needs no search. The current field, frontier and broad overview have the identities `research/current/agenda.md`, `research/current/frontier.md` and `research/current/overview.md`.

Phrase queries use double quotes inside the query, for example `--query '"semantic junction"'`. Ranking is lexical evidence, not a semantic relevance probability. If a useful body is missing, change the question, follow a source reference or module, ask a qualified participant, or use another source. Reranking the same candidates cannot select a body they exclude. Acquire `research/patterns/finding-useful-knowledge.md` for the acquisition-to-use account, optional embedding/decision participation, and derived-index currentness.

A broad question can use a compact qualified view or a local participant's answer as its own complete artifact. Its full underlying corpus need not enter the receiving context. `show` still returns the complete body actually requested; the navigation summary is not a replacement for it. Local detailed inquiry can reveal distinctions absent from the broad view and revise that view or its signals. Acquire `research/patterns/scoped-understanding.md` for selective fidelity, semantic junction probes and coverage of an intentionally partial representation.

For repeated searches of the same published edition, optional `prepare-index --output /your/work/search.sqlite` builds a contentless FTS5 index after verifying its sources. `discover --backend fts5 --index /your/work/search.sqlite --index-sha256 DIGEST --query TEXT` uses the exact digest from that build's receipt. This nominates from the pinned representation without rereading canonical bodies; acquire a selected body with `show`, or use `verify` for the whole library. Keep the index in caller-owned mutable work and retain its receipt. The [contract](CONTRACT.md#reuse-an-explicit-prepared-index) defines source/configuration binding, limits and rebuild/refusal behavior. Ordinary discovery keeps its per-call source verification.

When several questions are already available for that same edition and prepared scope, `discover-batch --queries questions.json --index /your/work/search.sqlite --index-sha256 DIGEST` shares one verified image across them. The input is a JSON array of 1–32 query strings. The result preserves their order and normal candidates, pins and coverage, while accounting for image acquisition once at the batch level. The image is closed before return; the next batch reacquires it. Input and complete result-envelope bounds are explicit in the [batch contract](CONTRACT.md#ask-several-questions-of-one-prepared-image). Separate discovery and direct `show` remain useful for individual questions.

To carry chosen understanding into another use, copy exact pins from `show`, `discover` or `modules` into a [selection](CONTRACT.md#select-complete-understanding), state its receiving purpose, and run:

```bash
python3 -B knowledge.py context --selection selection.json
python3 -B knowledge.py export --selection selection.json --edition my-use-1 --output knowledge.zip
python3 -B knowledge.py inspect --bundle knowledge.zip
python3 -B knowledge.py import --bundle knowledge.zip --store /tmp/knowledge-store
```

Create the caller-owned store directory first. Import returns the exact library path for subsequent `--library PATH` calls. It imports data, never instructions to execute, permissions or an active deployment. A receiving host assesses the package for its actual use.

Modules are overlapping, revisable views with purposes and explicit parent roles. Required members and dependencies define declared package closure; ordinary references and parents do not automatically become prerequisites. Compose a domain product from whichever views and complete bodies its use needs. A newly useful relationship can become another module after actual interpretation; the initial views here are authored starting points.

A complete published library, including its catalogue and objects, can be copied as one folder. This selected authoring-source directory is not that prepared library; use the operating release linked from the archive entrance for its shipped reader and catalogue. Acquire the included `prototype/README.md` body to understand optional execution arrangements in the wider prototype product. [Maintenance and migration](MIGRATION.md) explains source ownership, retired entrances and publication. Exact older occurrences remain recoverable at their declared source routes; their presence is distinct from current acceptance.

For developing observations and probes, acquire `research/patterns/developing-probes.md`. It connects the broader research field to the difference a return can make in an actual receiving operation. A selected product can be adequate for a general guide while a later current decision needs another source or live owner account; its declared closure does not silently include those future contributions.

Acquire `research/patterns/useful-closure.md` to distinguish whether current evidence determines an answer from whether it admits a useful common continuation. `research/current/frontier-settlement.md` accounts for every maintained frontier condition and stable implication without claiming to enumerate all future unknowns.
