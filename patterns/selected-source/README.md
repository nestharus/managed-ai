# Selected-Source Reader (`patterns/selected-source/`)

`patterns/selected-source/read_selected.py` reads explicitly nominated project files against expected selection and manifest digests, reporting exact observed identities while keeping unselected files explicitly unverified.

## CLI

```sh
python -B patterns/selected-source/read_selected.py \
  --root /path/to/project \
  --selection /path/to/selection.json \
  --path relative/file.md
```

## Assurance scope

- Verifies expected selection/manifest identities and exact bytes + SHA-256 of every requested path under ordinary, unfollowed paths.
- Leaves unselected remainder files explicitly unverified; use `domains/ai-system-building/operations/verify_selected_sources.py` for whole-store verification.
- Source identity establishes which bytes were read, not current semantic applicability or authority to act.
