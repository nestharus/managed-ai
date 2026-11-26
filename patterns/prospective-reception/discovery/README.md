# Prospective Reception Discovery Offers (`patterns/prospective-reception/discovery/`)

Reference implementations for the 16 discovery, inquiry, pricing, alignment, and receiver-adapter contracts declared in `../offers.json`.

## Indexed offers (`../offers.json`)

| Contract | Entry script | Focus |
| --- | --- | --- |
| `g60.changed-reads.v1` | `read_routes.py` | Readers requiring reconsideration after an input changes |
| `g60.label-cuts.v1` | `compare_cuts.py` | Agreement across reporting holders for an order label |
| `g60.probe-decision.v1` | `probe_plan.py` | Affordable observation selection to settle an action |
| `g60.history-inquiry.v1` | `history_inquiry.py` | Command sequences distinguishing equivalent histories |
| `g60.scoped-return.v1` | `scoped_return.py` | Minimal actionable reply preserving protected cases |
| `g60.handoff-memory.v1` | `handoff_memory.py` | Minimal state updatable from observed events |
| `g60.private-reply.v1` | `private_reply.py` | Private record + public reply across protected histories |
| `g60.coupling-bounds.v1` | `coupling_bounds.py` | Divergence bounds for paired public histories |
| `g60.decision-audit.v1` | `decision_audit.py` | Sequential cohort audit under bounded change |
| `g60.local-policy.v1` | `local_policy.py` | Pre-agreed local decisions meeting joint contracts |
| `g60.channel-replacement.v1` | `channel_replacement.py` | Observation substitution before receiving deadlines |
| `g60.shared-inquiry.v1` | `shared_inquiry.py` | Public observation dispatch for independent recipients |
| `g60.order-repair.v1` | `order_repair.py` | Enforceable ordering rights for concurrent peers |
| `g60.boundary-pricing.v1` | `boundary_pricing.py` | Conditional prices preserving cheapest valid combinations |
| `g60.peer-alignment.v1` | `peer_alignment.py` | Recipient decisions invariant under peer renaming |
| `g60.receiver-adapter.v1` | `receiver_adapter.py` | Supplier coverage of future buyer commands |

See [INTERFACE.md](INTERFACE.md) for turn CLI flags and [../README.md](../README.md) for the enclosing pattern study overview.
