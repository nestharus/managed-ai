# Examples

Standalone standard-library examples demonstrating Managed AI receiving, feedback, and multi-desk coordination boundaries. Keep mutable example state in a fresh directory outside this repository.

## Receiving example (`receiving_example.py`)

Drives `src/managed_ai/collective/__main__.py` (`Python 3.11+` / `SQLite 3.38+`) through three operations without embedding model inference or automatic retries:

- **Prepare:** creates distinct `producer` candidate and `receiver` observation/approval records under a unique `namespace`, capturing declared `sources` and `reconsider_from` actors (`g60-receiver-use/1`).
  ```sh
  python -B examples/receiving_example.py --engine src/managed_ai/collective --db /path/to/use.sqlite --operation prepare --input prepare.json
  ```
- **Take (admit later):** rechecks current source editions, approval pin, and required findings inside the local write transaction before recording `received-use`.
  ```sh
  python -B examples/receiving_example.py --engine src/managed_ai/collective --db /path/to/use.sqlite --operation take --input take.json
  ```
- **Resume:** reconciles an unfinished target via explicit `source_decisions` (`renew` / `release`) and notice `treatments` (`incorporated` / `retained-adequate` / `not-applicable`).

Exit codes: `0` accepted, `2` refused, `3` unresolved. Inspect uncertain delivery via `python -B -m managed_ai.collective --db ... receipt --actor ACTOR --command CMD`.

## Native feedback example (`native_feedback.py`)

Demonstrates wiring `engine/core` Attention feedback into a native store context/assess cycle without external dependencies.

## Aster canary desk (`aster-canary/`)

See [aster-canary/README.md](aster-canary/README.md) for the multi-desk terminal-return, collective-issue, and workbench-reconciliation walkthrough (`python -B examples/aster-canary/demo.py /path/to/fresh-study`).
