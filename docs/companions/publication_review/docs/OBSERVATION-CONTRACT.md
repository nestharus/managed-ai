# Publication Observation Contract (`publication_observer.py`)

`src/managed_ai_information/companions/publication_review/publication_observer.py` inspects declared files under caller-supplied `--root NAME=PATH` directories and compares observed bytes and SHA-256 digests against recorded publication receipts.

## Callable route and CLI

Import `observe_file(declaration_path, roots=...)` or run:

```sh
python -B src/managed_ai_information/companions/publication_review/publication_observer.py \
  --declaration /path/to/declaration.json \
  --root src=/path/to/root
```

## Declaration and path safety

- Declaration JSON is parsed strictly (duplicate keys and `NaN`/`Infinity` rejected).
- Every declared member path must be clean relative POSIX under a named root: backslashes, `..`, NUL bytes, absolute paths, and Windows drive specifiers (`C:foo`) are refused on all platforms.
- Every root ancestor, intermediate directory, and target file is inspected unfollowed (`lstat`, rejecting POSIX symlinks and Windows `FILE_ATTRIBUTE_REPARSE_POINT` / junctions).
- Sealed sources are never opened as ordinary bodies.

## Output and scope

- Returns a structured JSON observation account recording producer identity, declaration digest, per-root observed file statuses (`unchanged`, `changed`, `missing`), recorded receipt comparisons, and explicit scope limits.
- Mechanical digest observation does not establish semantic adequacy, automatic approval, or completeness outside the declared dependency list.
