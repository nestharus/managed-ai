# Prospective Reception (`patterns/prospective-reception/`)

Executable pattern studies exploring two-turn receiver handoffs, interrupted packet issuance, alignment before reuse, bounded coverage, joining progress, and discovery inquiry protocols.

## Structure

- `discovery/`: 16 offerable inquiry/alignment scripts indexed by `offers.json` (see [discovery/README.md](discovery/README.md) and [discovery/INTERFACE.md](discovery/INTERFACE.md)).
- `align-before-reuse/`, `coverage/`, `fixture/`, `joining-progress/`, `receiving-use/`: standalone Python studies and executable journals.

## Running local study checks

From `patterns/prospective-reception/`:

```sh
python -B -m unittest discover
```

These studies are self-contained reference patterns, not required runtime components of `engine/`.
