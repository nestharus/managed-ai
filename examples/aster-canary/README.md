# Resume a canary investigation

A worker has returned west-cohort latency. The release desk has a versioned rollout plan and its own observation of the rollback procedure. The receiving operation records a decision about that cohort. A separate process reads the decision and uses its measured latency and rollback command. This example runs from an exported system package or checkout with Python 3.14+ on Windows or POSIX. Choose a new directory to retain the independent desks.

```sh
python3 -B examples/aster-canary/demo.py /tmp/aster-canary-study
```

Each completed trial retains a native investigation, a sender outbox, a collective authority and a participant workbench. The output names the current decision, older draft base and incoming shared revision. The demo uses fixtures and caller-supplied trusted local identities. It exercises a local decision record. No rollout occurs.

| Work | Producer and condition |
|---|---|
| Carry a terminal worker return to collective custody | `Canary.stage` durably binds the exact native owner, packet, request, returned bytes, destination and command. `deliver` checks the current native inputs and uses the destination's exact command replay. A pre-commit exit leaves a pending intent. A lost reply after commit recovers the original destination receipt. |
| Authorize the joined decision | `Canary.issue` requires a delivered worker measurement, a current rollout-plan pin, a current desk-owned observation tied to that exact plan, an enabled current direction and no relevant open duty or required component review. It checks scope and the latency threshold. The native owner lock precedes the collective write transaction and is held through commit. Only this profile writes `canary_issues`. An ordinary collective publication cannot create that decision. |
| Inspect a late save | `Workbench.submit` freezes the participant, store, target, base, generation and body. A queued client action can pass its expected generation and base. `reconcile` gets the actual publication receipt. New typing stays private. `refresh` discovers an incoming head even before a successful local save. `merge` requires the expected generation and incoming revision and preserves the owner's chosen merged text. |
| Confirm selective exposure | `Workbench.receive` obtains the full canonical source body but commits only the requested interval and its digest to the private recipient’s store, atomically with the receipt. `delivered` returns the bytes the endpoint actually accepted. This does not establish which bytes a person saw or understood. |

The profile uses a typed west direction and obligations tagged with its profile or `all`. Untagged duties apply conservatively. It treats explicit reinstatement of identical policy values as reinstatement of the same direction. A policy requiring a fresh permission epoch needs an epoch in its direction record. The local lock covers cooperating G60 writers and the SQLite transaction covers this authority's records. An independently administered or physical writer needs a corresponding admission protocol. The external review’s Aster V2 has its own active-duty publication contract. This example installs a separate terminal-return profile and leaves the frozen V2 package and its stores intact.

See [examples/README.md](../README.md) for the receiving and feedback examples.
