# Source Admission (`patterns/source-admission/`)

`patterns/source-admission/admission.py` maintains a small SQLite-backed admission ledger binding candidate submissions to an explicit source epoch and receiver order (`Python 3.11+` / `SQLite 3.38+`). Keep the database outside the repository.

## CLI

```sh
python -B patterns/source-admission/admission.py --db /path/to/source.sqlite init --epoch revision-one --origin original
python -B patterns/source-admission/admission.py --db /path/to/source.sqlite open order-one --expected-epoch revision-one
python -B patterns/source-admission/admission.py --db /path/to/source.sqlite admit order-one candidate-one --source /path/to/source.txt
python -B patterns/source-admission/admission.py --db /path/to/source.sqlite close order-one --selected candidate-one
```

## Boundaries

- Tracks epoch transitions, candidate source digests, and receiver selection receipts under cooperative local ownership.
- Does not infer semantic equivalence across epoch changes; a changed source requires an explicit new admission or review decision.
