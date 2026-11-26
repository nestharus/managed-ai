# Shared optional companions

These adapters use the same central [engine](../engine/README.md). They contain no second native runtime or source corpus.

- [Workbench](workbench/docs/README.md): model/tool callbacks, explicit source acquisition and durable resumption.
- [Peer harness](peer/docs/PROTOCOL.md): caller-selected participant processes and bounded peer exchanges.
- [Publication review](publication_review/docs/PUBLICATION.md): declared source changes, receiving review and exact receipts.
- [Representation](representation/CONTRACT.md): opt-in structural representation checking and preparation.

Select the corpus and receiving store for the actual contribution. The trusted lexical reader CLI is `src/managed_ai/library/cli.py` (backed by `src/managed_ai/library/catalogue.py`); corpus directories hold data. The native store’s domain/source catalogues are the primary route when source exposure or typed records must follow that store’s selection. Companion registration and source discovery do not establish participant acquisition or semantic acceptance.
