# Publication Observation and Review (`src/managed_ai_information/companions/publication_review/`)

Portable standard-library tools for observing declared source changes, checking recorded publication receipts, and reviewing affected consumers from any working directory without mutating canonical sources.

## CLIs

Observe declared source inputs against recorded pins ([OBSERVATION-CONTRACT.md](OBSERVATION-CONTRACT.md)):

```sh
python -B src/managed_ai_information/companions/publication_review/publication_observer.py \
  --declaration /path/to/declaration.json \
  --root src=/path/to/source-root
```

Run a full publication review (joining observation output with `continuation_assessor.py`):

```sh
python -B src/managed_ai_information/companions/publication_review/publication_review.py \
  --declaration /path/to/declaration.json \
  --root src=/path/to/source-root \
  --retain-dir /path/to/new-receipt-dir
```

## Consumer and joined boundary

- `--root NAME=PATH` binds logical root names without pre-resolving symlinks or NTFS junctions; every ancestor and leaf is checked unfollowed before reads.
- `publication_review.py` imports `continuation_assessor` directly as a package module and records its live SHA-256 identity alongside every retained receipt (written atomically via temp directory rename in `_retain()`).
- Reported statuses distinguish unchanged declared inputs, changed declared inputs, missing/unreadable targets, and acceptance assertions.
- Declared dependencies cover only explicitly nominated paths; absence of reported change never certifies unexamined files or semantic compatibility.

Tests: `python -B -m unittest discover -s tests/companions/publication_review`.
