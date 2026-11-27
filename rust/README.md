# Native Rust Engine (`managed-ai-rs`)

This workspace provides the native Rust core and CLI for `managed-ai-information`, built directly on top of the Membus v2 binary protocol (`membus`) with zero SQLite dependencies and `#![forbid(unsafe_code)]`.

## Workspace Crates

- `crates/managed-ai-fs`: Cross-platform ordinary-file I/O, symlink and Windows reparse-point refusal, bounded reads, atomic writes, streaming SHA-256 hashing, and windowless subprocess construction (`CREATE_NO_WINDOW`).
- `crates/managed-ai-protocol`: Byte-exact canonical JSON encoder matching the Python wire format (`sort_keys=True`, `ensure_ascii=False`, `indent=2`, trailing LF), strict duplicate-key-refusing JSON parser, `g60-agent-system/0.1` record SHA-256 sealing and verification, and the `ManagedQuery` AST.
- `crates/managed-ai-store`: Async `StoreClient` over `membus::Client` supporting canonical record storage (`records/<id>`), atomic lease-guarded indexed transactions (`T_L`), Tantivy BM25 and literal substring search, sealed custody isolation, atomic filesystem export, and byte-exact filesystem-to-Membus parity verification.
- `crates/managed-ai-cli`: The `managed-ai-rs` command-line tool (`health`, `status`, `get`, `list`, `search`, `verify-parity`, `export-fs`, `verify-record`).

## Building and Testing

```bash
cargo test --manifest-path rust/Cargo.toml
cargo clippy --manifest-path rust/Cargo.toml --tests -- -D warnings
cargo build --manifest-path rust/Cargo.toml --release
```

## CLI Usage

```bash
managed-ai-rs --help
managed-ai-rs health --tcp 127.0.0.1:18422
managed-ai-rs status --pipe \\.\pipe\LOCAL\membus-v2 --namespace my-workspace
managed-ai-rs search --pipe \\.\pipe\LOCAL\membus-v2 --namespace my-workspace --query "schema" --limit 10
managed-ai-rs verify-parity --pipe \\.\pipe\LOCAL\membus-v2 --namespace my-workspace --workspace-root ./my-workspace
managed-ai-rs verify-record --file ./my-workspace/work/records/rec-1.json
```
