# Examples

Standalone standard-library examples demonstrating Managed AI receiving, feedback, bounded continuation support, and multi-desk coordination boundaries. All examples target **Python 3.14+** (primary runtime: **Python 3.15.2**). Keep mutable example state in a fresh directory outside this repository.

## Receiving example (`receiving_example.py`)

Drives `src/managed_ai/collective/__main__.py` (`Python 3.14+` / `SQLite 3.38+`) through three operations without embedding model inference or automatic retries. Ready-to-run sample inputs are shipped in [`sample_prepare.json`](sample_prepare.json) and [`sample_take.json`](sample_take.json):

- **Prepare:** creates distinct `producer` candidate and `receiver` observation/approval records under a unique `namespace`, capturing declared `sources` and `reconsider_from` actors (`g60-receiver-use/1`).
  ```sh
  python -B examples/receiving_example.py --engine src/managed_ai/collective --db /path/to/use.sqlite --operation prepare --input examples/sample_prepare.json
  ```
- **Take (admit later):** rechecks current source editions, approval pin, and required findings inside the local write transaction before recording `received-use`.
  ```sh
  python -B examples/receiving_example.py --engine src/managed_ai/collective --db /path/to/use.sqlite --operation take --input examples/sample_take.json
  ```
- **Resume:** reconciles an unfinished target via explicit `source_decisions` (`renew` / `release`) and notice `treatments` (`incorporated` / `retained-adequate` / `not-applicable`).

Exit codes: `0` accepted, `2` refused, `3` unresolved. Inspect uncertain delivery via `python -B -m managed_ai.collective --db ... receipt --actor ACTOR --command CMD`.

## Native feedback example (`native_feedback.py`)

Demonstrates wiring `src/managed_ai/core` and `src/managed_ai/store` feedback into a native store context/assess cycle and activated shared deployment without external dependencies:

```sh
python -B examples/native_feedback.py --help
python -B examples/native_feedback.py
```

## Continuing state support (`continuing_state_support.py`)

Standalone portable archive utility (`capture`, `verify`, `recover`) that snapshots a declared interpretation, its goal and obligation census, and recursively declared record-basis files into a SHA-256-verified ZIP projection without mutating or replaying an active owner:

```sh
python -B examples/continuing_state_support.py --help
```

## Aster canary desk (`aster-canary/`)

See [aster-canary/README.md](aster-canary/README.md) for the multi-desk terminal-return, collective-issue, and workbench-reconciliation walkthrough (`python -B examples/aster-canary/demo.py /path/to/fresh-study`).
