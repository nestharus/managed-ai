# Shared model and tool workbench

One canonical implementation serves direct source work, restartable work and peer turns. The callable controller is stored once in `../controller/`; shared core modules live in `../core/` (`io_utils.py`, `config.py`, `workbench_agent.py`, `model_bridges.py`, `source_tools.py`, `durable_agent.py`, `resumable_workbench.py`, `inspect_config.py`). Peer coordination lives in `../../peer/` (with `../../peer/COMPONENTS.json` pinning exact shared workbench sources) and publication review lives in `../../publication_review/`.

| Need | Entry and contract |
| --- | --- |
| Bounded model/tool loop | `../controller/agent_runtime.py` and [controller contract](CONTROLLER.md) |
| Read complete sources and work with local artifacts | `../core/workbench_agent.py` and [agent tools](AGENT.md), [source service](SOURCES.md) |
| Resume known callback outcomes without blind repetition | `../core/resumable_workbench.py`, [use](RESUME.md), [durability](DURABILITY.md), [contract](RESUME-CONTRACT.md) |
| Inspect a configuration without executing it | `../core/inspect_config.py` and [configuration guide](CONFIGURATION.md) |
| Contribute to a finite peer run | `../../peer/peer_workbench.py`, [use](../../peer/docs/PEERS.md), [contract](../../peer/docs/PEER-CONTRACT.md), [harness](../../peer/docs/PROTOCOL.md) |
| Observe changed declared dependencies | `../../publication_review/publication_review.py`, [use](../../publication_review/docs/PUBLICATION.md), [observation contract](../../publication_review/docs/OBSERVATION-CONTRACT.md) |

Supply `corpus_root` as the current source corpus, usually `domains/research/sources` in the package. Source discovery uses the bundled [source library](../../../engine/library/README.md) (`engine/library/reader/knowledge.py`). The workbench binds the effective reader and its local dependencies. The corpus contains source data. Known-path reads acquire complete Markdown bodies before an index is needed. Lexical discovery supplies nominations, not semantic applicability.

Set `__PRODUCT_ROOT__` in `../configs/example-resume-config.json` to `companions/workbench`, `__CORPUS_ROOT__` to the corpus, and `__WORK_ROOT__` to caller-owned work outside both packages. The examples contain empty duty arrays; supply the actual commissioned duties. They use a mailbox transport and no configured semantic receiver. An actual model and receiver remain caller-supplied.

```sh
python -B companions/workbench/core/inspect_config.py --config /absolute/config.json
python -B companions/workbench/core/resumable_workbench.py start --config /absolute/config.json
python -B companions/workbench/core/resumable_workbench.py inspect --state-dir /absolute/work/state
python -B companions/workbench/core/resumable_workbench.py resume --config /absolute/config.json
```

The controller, source and receiving APIs keep their existing behavioral boundaries. Consolidation changes code location, uses one shared component owner and binds the bundled search implementation. Old observed runs are evidence under their own inputs; this release does not rewrite their source bindings or claim their runtime state can be silently relocated.
