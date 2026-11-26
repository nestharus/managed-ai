# Continuation Core (`engine/core/`)

`engine/core/` is the domain-neutral continuation, exchange, attention, and cross-platform custody layer (version `0.4.2`, Python 3.11+ standard library only, cross-platform on Windows and POSIX).

## Components and CLIs

| Module / CLI | Role |
| --- | --- |
| `engine/core/cli/exchange_packets.py` (`g60-work`) | Source-bound continuation exchange: `init`, `context`, `receive`, `view`, `adopt`, `list`, `object`. Captures immutable content-addressed input/output objects and rechecks declared basis/target digests before adoption. |
| `engine/core/cli/track_attention.py` (`g60-attend`) | Scoped Attention companion: `track`, `view`, `assess`, `resolve`, `status` (full contract in [../docs/attention.md](../docs/attention.md)). |
| `engine/core/cli/collect_returns.py` (`g60-collect`) | Finite-command local carrier: `run`, `wait`, `status`, `collect`, `reconcile`. Dispatches one trusted command with `{context}`/`{packet}`, retains explicit lifecycle receipts, and never silently retries uncertain effects. |
| `engine/core/_platform.py` & `storage.py` | Cross-platform cooperative locking (`fcntl` on POSIX, `LockFileEx` on Windows), atomic writes (`fsync` + `replace`), strict JSON parsing (rejecting duplicate keys and non-finite numbers), and unfollowed symlink/NTFS reparse-point checks. |
| `engine/core/ownership.py` & `formats.py` | Safe relative POSIX paths (rejecting traversal, backslashes, NUL bytes, and Windows drive specifiers), layout ownership, and centralized wire-format constants. |
| `engine/core/exchange/` | `ObjectStore`, `Exchange`, packet preparation/inspection/receiving, and guarded publication. |

## Quickstart

Keep runtime state outside the repository (`--root /path/to/runtime`):

```sh
python -B engine/core/cli/exchange_packets.py --root /path/to/runtime init
python -B engine/core/cli/exchange_packets.py --root /path/to/runtime context t1 --request request.json
python -B engine/core/cli/exchange_packets.py --root /path/to/runtime receive t1 --reply reply.txt --producer local
python -B engine/core/cli/exchange_packets.py --root /path/to/runtime adopt t1 --target out.md --reason "verified"
```

## Boundaries and installation

- **Cooperative local owner:** Locks coordinate cooperating writers on one host; exit code `75` with JSON on stderr indicates `AdmissionContended`. Trusted commands inherit caller privileges.
- **No silent retries:** Timeouts or missing replies leave the original turn/command occurrence unresolved for explicit recovery via `status` / `reconcile`.
- **Optional editable install:** `engine/core/pyproject.toml` exposes `g60-work`, `g60-attend`, and `g60-collect` console scripts (`pip install -e engine/core` with no third-party dependencies).
- **Tests:** `python -B -m unittest discover -s tests/engine/core`. See [NOTICE.md](NOTICE.md) for source notice and [../docs/attention.md](../docs/attention.md) for the full Attention contract.
