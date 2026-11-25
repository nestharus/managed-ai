# Shared model and tool workbench

One canonical implementation serves direct source work, restartable work and peer turns. The callable controller is stored once in `controller/`; `workbench_agent.py`, `model_bridges.py` and `source_tools.py` are shared by the direct, resumable and peer entrances. `COMPONENTS.json` binds the exact current shared sources.

| Need | Entry and contract |
| --- | --- |
| Bounded model/tool loop | `controller/agent_runtime.py` and [controller contract](CONTROLLER.md) |
| Read complete sources and work with local artifacts | `workbench_agent.py` and [agent tools](AGENT.md), [source service](SOURCES.md) |
| Resume known callback outcomes without blind repetition | `resumable_workbench.py`, [use](RESUME.md), [durability](DURABILITY.md), [contract](RESUME-CONTRACT.md) |
| Inspect a configuration without executing it | `inspect_config.py` and [configuration guide](CONFIGURATION.md) |
| Contribute to a finite peer run | `peer_workbench.py`, [use](PEERS.md), [contract](PEER-CONTRACT.md), [harness](../harness/PROTOCOL.md) |
| Observe changed declared dependencies | `publication_review.py`, [use](PUBLICATION.md), [observation contract](OBSERVATION-CONTRACT.md) |

Supply `corpus_root` as the current source corpus, usually `domains/research/sources` in the package. Source discovery uses the bundled [source library](../../engine/runtime/source_library/README.md) (`engine/runtime/source_library/knowledge.py`). The workbench binds the effective reader and its local dependencies. The corpus contains source data. Known-path reads acquire complete Markdown bodies before an index is needed. Lexical discovery supplies nominations, not semantic applicability.

Set `__PRODUCT_ROOT__` in examples to this directory, `__CORPUS_ROOT__` to the corpus, and `__WORK_ROOT__` to caller-owned work outside both packages. The examples contain empty duty arrays; supply the actual commissioned duties. They use a mailbox transport and no configured semantic receiver. An actual model and receiver remain caller-supplied.

```sh
python -B inspect_config.py --config /absolute/config.json
python -B resumable_workbench.py start --config /absolute/config.json
python -B resumable_workbench.py inspect --state-dir /absolute/work/state
python -B resumable_workbench.py resume --config /absolute/config.json
```

The controller, source and receiving APIs keep their existing behavioral boundaries. Consolidation changes code location, uses one shared component owner and binds the bundled search implementation. Old observed runs are evidence under their own inputs; this release does not rewrite their source bindings or claim their runtime state can be silently relocated.
