# Continuing State (`patterns/continuing-state/`)

`patterns/continuing-state/support.py` captures, verifies, and recovers exact supporting files and native obligation state relative to a saved base without copying an entire store. Works on Python 3.11+ (Windows and POSIX).

## CLI

```sh
python -B patterns/continuing-state/support.py capture --root /path/to/owner --spec /path/to/spec.json --output /path/to/support.zip
python -B patterns/continuing-state/support.py verify --archive /path/to/support.zip --sha256 EXPECTED_SHA256
python -B patterns/continuing-state/support.py recover --archive /path/to/support.zip --sha256 EXPECTED_SHA256 --to /path/to/fresh-dir
```

## Guarantees and bounds

- Captures declared ordinary files under `--root` in deterministic filename order, verifying exact bytes, SHA-256, and native obligation ordering before sealing the ZIP archive.
- `verify` and `recover` require the exact expected archive SHA-256, reject symlinks/reparse points and path traversal, and recover only into a fresh target directory.
- Mechanical recovery preserves exact captured bytes; the receiving owner still judges current applicability.
