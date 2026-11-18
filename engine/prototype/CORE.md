# Continuation support 0.4.1

A standalone, standard-library Python package that keeps selected inputs, actual returns and public work accounts usable across local invocations. The caller supplies the task, interpretation and authority. The package supplies command-return collection, a source-bound continuation exchange, and caller-invoked attention feedback.

## Start with the contribution you need

| Need | Available means | Contract or example |
|---|---|---|
| Produce or revise an artifact in a context that already has the needed material | Use the existing interpreter and ordinary files directly | No extra packet or attention record is required |
| Keep nominated questions, obligations and wider alternatives available during continuing work | `attend.py`: scoped feedback, public accounts and captured sources | [Attention](docs/attention.md) |
| Give another invocation selected material, retain its actual return, or reuse an unpublished proposal | `work.py`: source-bound context and return exchange, with optional single-file publication | [Exchange](docs/exchange.md) |
| Recover the inputs that produced an earlier return | `work.py context PACKET`: validated original context, even when current workspace files are unavailable | [Choose the receiving view](docs/exchange.md#choose-the-receiving-view) |
| Collect the actual result of a trusted local command across invocations | `run.py`: explicit admission, dispatch and return or uncertainty | [Collector](docs/contract.md) |

These are independently chosen means, not mandatory phases. An exchange command adapter already uses the collector; it does not need a second admission through `run.py`. The attention companion uses the exchange's storage but does not require a model packet for each question or account.

The [architecture guide](docs/architecture.md) shows how the components connect to a caller, to retained material and to actual effects. The [complete local example](docs/quickstart.md) exercises attention, return receipt, reuse and publication using explicitly authored text. It needs no model account or credentials. For integration, the [API and implementation map](docs/api.md) identifies the supported interfaces, source modules and responsibilities that stay with the host.

## Requirements and boundaries

Python 3.10 or later on POSIX, with `fcntl`, directory `fsync`, writable local storage and process detachment. Native Windows is not supported. There are no third-party runtime dependencies. Use one cooperative owner and trusted finite foreground commands with bounded output.

Runtime roots must be outside this distribution. An exchange root and its working directory must be disjoint. State created during use belongs to the caller, not in this source folder. Use `-B` for source-tree invocations as below to avoid writing Python caches into the extracted package. Hashes bind bytes in trusted local storage; they do not authenticate a hostile writer or certify meaning.

Receipt does not authorize publication. A completed command can have a nonzero exit or fail its purpose. An interrupted attempt is not automatically retried. The collector has no general timeout, cancellation or process-tree guarantee; the exchange publishes files individually, not as a multi-file transaction. No model provider, autonomous relevance selection, scheduler or sandbox is installed. See each contract before relying on its behavior.

## Run and verify

From this folder:

```bash
python3 -B attend.py --help
python3 -B work.py --help
python3 -B run.py --help
python3 -B -m unittest discover -s tests -v
```

Tests use temporary external directories and actual local subprocesses. They do not invoke a model or establish semantic efficacy.

## Install or build

The supplied wheel in `dist/` installs the commands `g60-attend`, `g60-work` and `g60-collect`. Run the source launchers directly when installation is unnecessary.

An optional rebuild requires setuptools and wheel in the build environment:

```bash
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir /external/wheels .
```

Use a disposable source copy for building; generated build directories are not part of the clean distribution. Version 0.4.1 adds explicit original-context recovery and makes packet receipt inspect its known carrier job directly. It preserves existing stored formats and effect boundaries.
