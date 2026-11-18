# Consumer and joined boundary

The captured producer boundary is adopted: `publication_observer.observe(declaration, roots=None, *, base_dir=None)` and `observe_file(declaration_path, roots=None)` return `{schema, current, origins}`. The declaration schema is `publication-declaration/1`; its complete reference belongs to `OBSERVATION-CONTRACT.md`.

Reviewer owns `publication_review.py`, exact `continuation_assessor.py`, `README.md`, and this interface note. Observer owns `publication_observer.py` and `OBSERVATION-CONTRACT.md`. Temporary peer copies used for local integration are not reviewer submissions.

The consumer exposes:

- `assess_documents(previous, current, *, assessor_path=None)`: exact assessor result, no observation performed.
- `review_observations(previous, observation, *, assessor_path=None)`: full report containing the complete detached observation, current/origins, exact assessment, and explanations. Caller-supplied observations are not independently revalidated.
- `review(previous, declaration, roots=None, *, base_dir=None, assessor_path=None)`: actual producer observation followed by exact assessment.
- `review_files(previous_path, declaration_path, roots=None, *, assessor_path=None)`: explicit JSON input route; relative declaration bindings use the declaration's directory.

The combined CLI accepts `--previous` and exactly one of `--declaration`, `--observation`, `--current`. `--root NAME=PATH` belongs to `--declaration`; `--origins` belongs to `--current`. Optional `--assessor` must have the exact accepted bytes. Default emits the complete report to stdout; `--output-dir` creates a fresh requested directory containing previous, current, origins, complete observation, exact assessment, and full report JSON. Existing outputs and missing output-parent directories are refused.

The immutable assessor identity is 7,100 bytes, SHA-256 `3109ffeb01032d2f4dbc3afa6df2b05d9bb8a1f68f51561c86d2a7efbd6e1f67`. The consumer verifies this identity before loading its unchanged module and directly invokes `assess`. Installed modules are compiled without implicit bytecode-cache writes; declared input content is never executed.

Every prior declared input remains the previous basis. Ordinary current artifact observations permit detecting changed intermediate outputs without asserting acceptance. An optional producer acceptance assertion is a separate recorded-evidence check against the named actual bytes. The previous accepted-input digest is never replaced with the current one. Recorded selection currentness means the owner-selected recorded basis, not a fresh search or catalogue refresh.

This interface is implemented and checked locally against the captured observer revision 1. Local feedback is distinct from the independent receiver's later whole-product result; the original-history duties remain open.
