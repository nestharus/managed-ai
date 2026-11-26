# Method Invocation (`patterns/method-invocation/`)

`prepare.py` and `check_preparation.py` prepare and verify a source-bound method invocation from verified library sources without executing unreviewed external code.

## CLI

Verify the source library first, then prepare and check a method invocation:

```sh
python -B engine/library/cli.py --library /path/to/library verify
python -B patterns/method-invocation/prepare.py --spec /path/to/spec.json --output /path/to/new-dir
python -B patterns/method-invocation/check_preparation.py --prepared /path/to/new-dir
```

## Contract

- Pins the exact source bodies, `AGENTS.md`, `README.md`, and [representation contract](../../companions/representation/CONTRACT.md) inputs declared in the spec.
- Rejects missing pins, modified sources, symlinks/reparse points, and writes inside canonical source trees.
- Preparation produces a reviewable invocation bundle; execution remains explicitly caller-authorized.
