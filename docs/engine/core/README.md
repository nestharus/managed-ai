# Continuation Core (`src/managed_ai/core/`)

`src/managed_ai/core/` is the domain-neutral continuation, exchange, attention, and cross-platform custody layer (version `0.4.2`, Python 3.11+ standard library only, cross-platform on Windows and POSIX).

## Components and CLIs

| Module / CLI | Role |
| --- | --- |
| `src/managed_ai/core/cli/exchange_packets.py` (`g60-work`) | Source-bound continuation exchange: `init`, `context`, `receive`, `view`, `adopt`, `list`, `object`. Captures immutable content-addressed input/output objects and rechecks declared basis/target digests before adoption. |
| `src/managed_ai/core/cli/track_attention.py` (`g60-attend`) | Scoped Attention companion: `track`, `view`, `assess`, `resolve`, `status` (full contract in [../attention.md](../attention.md)). |
| `src/managed_ai/core/cli/collect_returns.py` (`g60-collect`) | Finite-command local carrier: `run`, `wait`, `status`, `collect`, `reconcile`. Dispatches one trusted command with `{context}`/`{packet}`, retains explicit lifecycle receipts, and never silently retries uncertain effects. |
| `src/managed_ai/platform/` & `core/storage.py` | Unified stdlib OS platform adapter (`PlatformAdapter`, Windows `LockFileEx`/POSIX `fcntl`, atomic writes, streaming `sha256_file`, strict JSON, reserved-name and unfollowed symlink/NTFS reparse-point checks) with thin `core/_platform.py` compatibility wrapper. |
| `src/managed_ai/core/ownership.py` & `formats.py` | Safe relative POSIX paths (rejecting traversal, backslashes, NUL bytes, and Windows drive specifiers), layout ownership, and centralized wire-format constants. |
| `src/managed_ai/core/exchange/` | `ObjectStore`, `Exchange`, packet preparation/inspection/receiving, and guarded publication. |

## Quickstart

Keep runtime state outside the repository (`--root /path/to/runtime`):

```sh
python -B src/managed_ai/core/cli/exchange_packets.py --root /path/to/runtime init
python -B src/managed_ai/core/cli/exchange_packets.py --root /path/to/runtime context t1 --request request.json
python -B src/managed_ai/core/cli/exchange_packets.py --root /path/to/runtime receive t1 --reply reply.txt --producer local
python -B src/managed_ai/core/cli/exchange_packets.py --root /path/to/runtime adopt t1 --target out.md --reason "verified"
```

## Boundaries and installation

- **Cooperative local owner:** Locks coordinate cooperating writers on one host; exit code `75` with JSON on stderr indicates `AdmissionContended`. Trusted commands inherit caller privileges.
- **No silent retries:** Timeouts or missing replies leave the original turn/command occurrence unresolved for explicit recovery via `status` / `reconcile`.
- **Optional editable install:** Root `pyproject.toml` exposes `managed-ai`, `managed-ai-deploy`, `managed-ai-library`, `g60-work`, `g60-attend`, and `g60-collect` console scripts (`python -m pip install -e . --no-build-isolation` with zero third-party runtime dependencies).
- **Tests:** `python -B -m unittest discover -s tests/core`. See [NOTICE.md](../../../src/managed_ai/core/NOTICE.md) for source notice and [../attention.md](../attention.md) for the full Attention contract.
