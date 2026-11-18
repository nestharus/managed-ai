# Develop the next investigation

## Replace a buyer command with a sequence of supplier actions

Look up `g60.receiver-adapter.v1` with `python3 -B offers.py . --contract g60.receiver-adapter.v1` in the extracted operating directory. Run the returned entry with a JSON file as its only argument or send the commission on standard input. This example requires a sheet and a debit on every order. A private reservation can be retained after cancellation for the next order.

```json
{
  "start": "ready",
  "source": {"ready": {"order": ["ready", ["sheet", "debit"]]}},
  "possible": ["open"],
  "fees": {"prepare": 1, "commit": 1},
  "target": {
    "open": {"prepare": ["held", [], ""]},
    "held": {"commit": ["open", ["sheet", "debit"], ""]}
  },
  "cancellation": true
}
```

Each buyer state lists the commands the buyer may issue. Each command names the next state and ordered public effects. A supplier row gives an enabled action's next state, emitted public effects and private observation. Every enabled action has a positive integer operating fee. Multiple possible initial supplier states describe hidden configurations. The same action must be enabled in all states still possible. The adapter can branch only on the public effects and private signal it receives. Buyer requests run sequentially. Setting `cancellation` to true permits immediate withdrawal after any proper native-step boundary. The cancelled request owes zero public effects and the previous buyer state must remain serviceable. The final native step and return to the caller share a boundary.

On this commission the minimum worst-case fee per request is two. Use the entry matching the buyer state, current belief index and command. At each node perform its action once, match the actual effects and signal to one reply, then follow its node index. A stop transfers to the entry's next buyer state with the leaf's belief. On an allowed cancellation transfer to the original buyer state using the most recent reply node's belief. Debit only the fees for actions actually performed. The solver supplies the choices and budget. The action owner enforces permissions and the receiving host owns delivery, transaction boundaries and synchronization.

`impossible` returns elimination rounds with a challenging buyer command for each losing state pair. A qualified result includes the elimination rounds at one unit below its peak unless the peak is zero. The bound minimizes the largest fee of a single request over an arbitrarily long legal sequence. An empty command can require no action. At most 256 reachable information states are supported. Changed supplier state, lost replies, overlapping requests and failures inside a native action require a new operating contract. The workbench retains the physical receiving trials under `evidence/receiver-adapters`.

## Align the records two peers actually use

Resolve `g60.peer-alignment.v1` with `python3 -B offers.py . --contract g60.peer-alignment.v1` from the extracted G60-system-use directory. Give its operating entry a JSON commission. Nodes carry source-provided types, directed edges carry relation labels, and a receiving use selects the observation an authorized right-side owner can return. A closed relation warrants comparisons of present and absent edges; positive-only coverage licenses the observed edges. The default shape is a bijection. Use `embedding` when the target may include other nodes. Each candidate maps distinct left identities to distinct target identities.

```json
{
  "relations": {
    "provides": "closed"
  },
  "left": {
    "nodes": {
      "agent-a": "desk",
      "agent-b": "desk",
      "patch": "source",
      "paper": "source"
    },
    "relations": {
      "provides": [
        [
          "agent-a",
          "patch"
        ],
        [
          "agent-b",
          "paper"
        ]
      ]
    }
  },
  "right": {
    "nodes": {
      "desk-x": "desk",
      "desk-y": "desk",
      "stock-1": "source",
      "stock-2": "source"
    },
    "relations": {
      "provides": [
        [
          "desk-y",
          "stock-1"
        ],
        [
          "desk-x",
          "stock-2"
        ]
      ]
    }
  },
  "readings": {
    "lane": {
      "stock-1": "gold",
      "stock-2": "silver"
    }
  },
  "uses": {
    "route": {
      "left": [
        "patch"
      ],
      "read": "lane"
    }
  },
  "probes": {
    "ask-owner": {
      "left": "agent-a",
      "cost": 2
    },
    "open-source": {
      "left": "patch",
      "cost": 3
    }
  },
  "reference": {
    "agent-a": "desk-y",
    "agent-b": "desk-x",
    "patch": "stock-1",
    "paper": "stock-2"
  },
  "mode": "panel"
}
```

The two desks have indistinguishable unanchored roles, while the two possible destination sources convey different route readings. This reference's panel buys `ask-owner` for two. Its answer must be `desk-y` to warrant `gold` on that panel's reference branch. To compare the target use on all admissible maps, choose `qualify`. If two maps imply different actions the output gives both. Choose `policy` to price an observation tree that works across the possible answers and minimizes its worst bill. Ordered readings preserve order; a bag use can tolerate exchanges of roles that do not change its resulting multiset. Separate operating authority must actually supply the advertised answer from the applicable owner and epoch. A work limit may leave a decision unresolved.


## Price open scopes and exercise an award

From the extracted operating directory resolve `g60.boundary-pricing.v1` through `python3 -B offers.py . --contract g60.boundary-pricing.v1`. Pass a commission JSON file to the discovered program. This example keeps the purchase price conditional on a quote's availability, then exercises the live row. Each named field has a finite set of values. A source row's coordinates follow its declared scope order. An omitted row means the owner has no offer there.

```json
{
  "space": "revision-quote/1",
  "domains": {
    "quote": [
      "absent",
      "live"
    ],
    "method": [
      "local",
      "bundle"
    ]
  },
  "open": [
    "quote"
  ],
  "factors": {
    "supplier": {
      "version": "tariff-1",
      "scope": [
        "quote",
        "method"
      ],
      "rows": [
        {
          "at": [
            "absent",
            "local"
          ],
          "cost": 3
        },
        {
          "at": [
            "live",
            "local"
          ],
          "cost": 3
        },
        {
          "at": [
            "live",
            "bundle"
          ],
          "cost": 1
        }
      ]
    },
    "activation": {
      "version": "fee-1",
      "scope": [
        "method"
      ],
      "rows": [
        {
          "at": [
            "local"
          ],
          "cost": 0
        },
        {
          "at": [
            "bundle"
          ],
          "cost": 1
        }
      ]
    }
  },
  "tree": [
    "supplier",
    "activation"
  ],
  "award": {
    "quote": "live"
  }
}
```

The absent quote costs three. When the quote is live the accepted bundle costs one plus one for activation. The optional tree tells the calculator which peers should join first. An external field must stay in `open` while a later buyer can set it; dropping a field once every *declared* owner touching it has joined assumes the catalog and cut are complete. Issuer versions and material commitments are inputs to the analysis. Local witnesses direct later exercise; the purchase, funding and authentication happen at their actual owners.

The adjacent `boundary_messages.py` supports separate desks. The `catalog SPEC` operation creates the common catalog. For `leaf CATALOG OWNER ROWS`, pass just that owner's row array as the last JSON file. A leaf or `join CATALOG LEFT RIGHT` returns a `public` price message and `private` witness journal. Pass public messages as LEFT and RIGHT and keep the journals with their issuing peers. `compare LEFT RIGHT` reports equality, dominance or crossing quotations on a common interface, with a distinguishing selection for different prices. `route JOURNAL AWARD` takes an award JSON object containing the public message's `message` seal and the chosen row coordinates `at`. A join journal routes to child messages and coordinates; a leaf journal routes to the owner's chosen row. Check the job, authority and validity epoch externally. Matching content digests detect accidental substitution but cannot establish a supplier's right to act or the truth of its claim.

The adjacent `boundary_recover.py JOB` can plan acquisition from qualified message descriptors before buying their bodies. Supply a `catalog`, a `purpose` of `quote` or `award`, and named `cards` declaring semantic space, covered members and versions, ports, fee, witness availability and message seal. For an award the chosen cards must retain reconstructible witnesses. The planner finds the minimum sum of acquisition fees among disjoint usable covers. After purchase validate the actual returned messages against the catalog, seals and witnesses. The acquisition bill covers the descriptors' retrieval terms; downstream execution and action rights have their own prices.

For owners whose private views alone cannot select compatible actions, `boundary_award.py JOB` accepts `pricing`, a map of receiver `roles` to action fields, observation `costs`, and `cases` supplying the available views, offers and weights at each feasible open context `at`. In default `threshold` mode, `max_extra` bounds permitted material cost above each context's minimum and the native shared inquiry minimizes its fee within that allowance. In `mode: total`, the optimizer minimizes the weighted sum of paid observation fees and realized material costs, subject to an optional per-context excess cap. A capped search may return separate lower and upper bounds. Compare with buying the input from its current owner or sending a complete offer directly when that purchase is already permitted.

## Preserve a reference order while allowing parallel arrival

Discover `g60.order-repair.v1` in the compact with `python3 -B offers.py . --contract g60.order-repair.v1`. Run its returned entry with one JSON file. Event IDs identify individual occurrences. For each participating receiver, give its initial state, every named command's receipt and successor in each state, and the commands a later buyer may use. The reference runs every event exactly once. Optional `before` edges are already enforceable. A priced offer may contain multiple edges and threshold gates; each gate demands its stated number of distinct earlier occurrences before the target event. All offered constraints must admit the reference.


```json
{
  "events": {
    "proof-A": {
      "shipper": "verify"
    },
    "proof-B": {
      "shipper": "verify"
    },
    "ship": {
      "shipper": "ship"
    }
  },
  "reference": [
    "proof-A",
    "proof-B",
    "ship"
  ],
  "receivers": {
    "shipper": {
      "initial": "pending",
      "states": {
        "pending": {
          "verify": [
            "filed",
            "ready"
          ],
          "ship": [
            "hold",
            "pending"
          ]
        },
        "ready": {
          "verify": [
            "filed",
            "ready"
          ],
          "ship": [
            "paid",
            "ready"
          ]
        }
      },
      "follow": [
        "ship"
      ]
    }
  },
  "offers": {
    "one-receipt": {
      "cost": 1,
      "gates": [
        {
          "before": [
            "proof-A",
            "proof-B"
          ],
          "need": 1,
          "event": "ship"
        }
      ]
    },
    "wait-A": {
      "cost": 2,
      "edges": [
        [
          "proof-A",
          "ship"
        ]
      ]
    }
  },
  "preserve": [
    [
      "proof-A",
      "ship"
    ],
    [
      "proof-B",
      "ship"
    ]
  ]
}
```

In this example either inspector's verified receipt permits shipment. The fee-one gate is safe with both arrival orders of the inspectors, including a shipment between their returns. With reference safety alone the fee-two fixed edge also works. This buyer additionally supplies two `preserve` prefixes. Shipping must remain possible as soon as either qualifying receipt arrives while the other is still late. The fixed-A offer excludes the B-first prefix. The returned `excluded_offers` identifies that conflict. The solver prices offer fees. An owner who cares about calendar deadlines should measure the resulting permitted arrival times and authenticate the receipts accepted by each gate. Designating an unqualified receipt as a proof can make the executable model precise while the shipping result is wrong.

An optimal result gives the selected offers and fee. A counterexample can distinguish an immediate per-event receipt or a future command sequence that separates the two final states. With no sufficient combination the solver returns a failing legal order. On reaching `work_limit`, inspect the proved lower bound and any feasible incumbent and upper bound. A changed receiver, future command vocabulary, availability of signed receipts or allowed occurrence set calls for a new commission. The value assumes each promised control can be implemented at its stated fee.

After commissioning, run the companion `order_desk.py issue SPEC JOB` and save the issued plan. Consult `order_desk.py ready PLAN EVENT RECEIPTS` before committing an occurrence. Each accepted receipt records the job, commission digest, occurrence name and committed effect ID. The local pure decision returns ready, waiting with the remaining gate requirements, or the already committed effect. The owner authenticates receipts, checks the current source qualification, and makes actual readiness, debit and commit atomic where required. A job ID or digest alone cannot authenticate an issuer or grant exclusive spending authority.

Use a real partial artifact. Name something a receiver might do differently with it, a competing consequence, and the missing participation that could distinguish them. Freeze those before looking for an answer when timing matters. A known qualified source can go straight into an issue. A local model, direct reader, tool, external owner or independent participant supplies the actual interpretation or observation.

Run `python -B turn.py STATE ACTION SPEC.json` beside this file. `STATE` is a caller-owned SQLite filename outside the distribution. Supply the actual returned `id` wherever a later spec names a prior record. File paths inside a spec are relative to that spec. Use `show` on an observation ID in `later_different_verdicts` in the originating journal to inspect its result and evidence pin. At most two representatives appear, newest first. Use the underlying journal for earlier examples with the same verdict. New transfers see subsequent observations; earlier issues retain their captured contents.

The interface works without network access or packages beyond Python 3.10 and SQLite. Consult [the exact spec fields](INTERFACE.md).

| Current question | Action and return |
|---|---|
| What should the next encounter resolve? | `open` saves the target, gap, prediction, rival, exact partial and intended query. It works before a source identity or dimension name exists. |
| What available body might help? | `lookup` invokes the trusted G60 reader with the frozen query. It returns up to three pinned identities, titles, a library digest and scope counts. Rephrase by opening another prospect. Native `show ID --text` acquires the complete selected body. An external source or a direct observation can also fill the gap. |
| What does this receiver actually need to see now? | `issue` returns a bounded packet with the prospective partial, exact windows from one to three sources and one to four use/decision/authority entries. Decide what to deliver, to whom, and whether revealing these inputs is suitable for that trial. |
| What happened in this particular use? | `observe` addresses one named use and preserves the exact evidence, the observer's interpretation and its verdict. Separate uses get separate observations. A changed partial can motivate another prospect. |
| Can existing work help a different decision? | `transfer` projects a saved investigation into a new prospect and one local use. State why it could carry and which observation could defeat that warrant. Select a subset of prior source roles if appropriate. The returned issue preserves the earlier result and shows whether the authority bytes changed. For each different verdict found later for that same issue and use, the transfer carries its newest observation ID and verdict. Obtain the new use's own return. |
| Is the original decision actually the same, or has something changed? | An issue with `relation: reuse` checks the same narrative, source, partial, decision and authority against a supporting observation. This is replay. Use `reopen` for a changed basis and `split` for independent or jointly changed participation, with a reason and selected current sources. Several children may share a parent. |

When several later findings with the same verdict may affect different parts of an incoming job, ask the original custodian for `catchup` after the known observation. Follow the returned continuation through its pinned local cut, including empty partial pages. Retrieve the selected observations there and exercise the new use. The ordinary transfer remains the smaller choice for a few representative outcomes. For a single already-known finding, use `show` directly. [Scoped history request](INTERFACE.md#read-a-scoped-history-after-a-frozen-observation)

### Which observations should several operators buy together?

Resolve `g60.shared-inquiry.v1` with `python3 -B offers.py . --contract g60.shared-inquiry.v1` from the extracted G60-system-use directory. Run the discovered entry with one JSON file. This example buys a single calibration result delivered privately to both operators. The purchaser sees only that it was filed.

```json
{
  "owners":["assembler","labeler"], "costs":{"calibrate":2}, "budget":2,
  "worlds":{
    "blue":{"views":{"assembler":"job","labeler":"job"},"allowed":{"assembler":["left"],"labeler":["blue"]},
            "offers":{"calibrate":{"public":"filed","private":{"assembler":"L","labeler":"B"}}}},
    "gold":{"views":{"assembler":"job","labeler":"job"},"allowed":{"assembler":["right"],"labeler":["gold"]},
            "offers":{"calibrate":{"public":"filed","private":{"assembler":"R","labeler":"G"}}}}
  }
}
```

Save the output as policy.json. Its mean and worst costs are both 2. A public dispatcher purchases calibrate and stops on filed. At the gold installation the assembler chooses right on its private R and the labeler chooses gold on its private G. Each worker may read the public history. A private result cannot pick a different public purchasing branch. A missing offer in a possible world prevents scheduling it until previous public observations exclude that world.

```sh
python3 -B adapters/prospective-reception/discovery/inquiry_desk.py project policy.json > dispatcher.json
python3 -B adapters/prospective-reception/discovery/inquiry_desk.py project policy.json --owner assembler > worker.json
python3 -B adapters/prospective-reception/discovery/inquiry_desk.py step dispatcher.json public.json
python3 -B adapters/prospective-reception/discovery/inquiry_desk.py step worker.json local.json
```

The initial dispatcher record is `{"public":[]}`. After the purchase it accepts only `{"public":[["calibrate","filed"]]}`. A worker awaiting a purchase may inspect the public route. At a leaf it needs its initial view and an explicit receipt for every acquired query. For example `{"public":[["calibrate","filed"]],"view":"job","private":{"calibrate":"R"}}` lets the gold assembler act. Use JSON null for an acknowledged paid query with no private payload. Omission is an incomplete leaf record. Give each caller only its own projection. These projections contain a policy, so disclosure of the policy itself must be authorized. Application code binds signs of receipt, charges and acknowledgments to the durable order and handles the effects of its action.

Positive integer world weights default to one. Under objective `mean` the solver minimizes expected acquisition cost and breaks ties by worst path cost. Under `worst` it reverses those objectives. The optional budget caps expenditure on every path. An offer can be used once per path. To constrain how local actions combine, give a top-level `scopes` mapping from clause names to ordered owners; each world's `joint` supplies the allowed action tuples for every named clause. An optimal result settles all specified individual and joint obligations in every declared world. An infeasible result exhausts the admissible deterministic purchases under that budget. Hitting a state or assignment cap returns resource-limit and gives no feasibility verdict. The finite table assumes the environment already specifies what an acquired offer can return.

### Can another source deliver the promised observations in time?

Resolve `g60.channel-replacement.v1` in the offer catalogue. Pass its discovered entry one JSON file. A world records what the source can report and what the buyer was promised. A tape is the ordered sequence of observations on one run. Positive integer weights within each world and channel are normalized separately. The following purchase requires the right label before dispatch. The available source learns it one step later.

```json
{
  "worlds": {
    "blue": {
      "source": [{"tape":["wait","blue"],"weight":1}],
      "target": [{"tape":["blue","closed"],"weight":1}]
    },
    "gold": {
      "source": [{"tape":["wait","gold"],"weight":1}],
      "target": [{"tape":["gold","closed"],"weight":1}]
    }
  },
  "timing": "online",
  "tolerance": "0"
}
```

Online rules can use only the source prefix available when the next target symbol must be emitted. Here the earliest decision costs a worst-world error of 1/2. Changing timing to `after` lets the converter read the full source tape before answering and lowers the error to zero. The error is total variation between target and simulated transcript laws, maximized over the named worlds. It bounds the loss for a receiving payoff in the range zero to one under the same admissible use.

Read `converter` as a lottery over complete deterministic rule tables. Draw a table once for the run. Under online timing, a rule’s `after` and `emit` entries give the source and target prefixes. The `countercommission` gives weighted worlds, rewards on complete outputs, the value of honoring the target and the best admissible source rule. If the returned interval still overlaps both sides of your tolerance, the result is unresolved. The `forbid` list can name specific complete source and target tape pairs that a converter may never realize, even with randomization. These restrictions can eliminate every admissible converter.

The command accepts 1–16 worlds, a common horizon of 1–8 symbols and up to 64 distinct tapes on either side. The optional `round_limit` is 1–96, default 64. With `stop_on_decision` set to true, a certified threshold decision can stop the search before the endpoints meet. Future buyer actions changing the source law require a new model. Check signed evidence, funding and final delivery with their owners.

### Buying a policy that separate operators can execute

Resolve `g60.local-policy.v1` in the offer catalogue. The entry accepts one local JSON file. For example, a desk receives the same slip for either of two buyer states. Its chosen article must be correct at least half the time in each state.

```json
{
  "roles": [{"id":"desk","views":{"slip":["blue","gold"]}}],
  "worlds": {
    "wants-blue":{"views":["slip"],"outcomes":[{"actions":["blue"],"scores":{"exact":1}},{"actions":["gold"],"scores":{"exact":0}}]},
    "wants-gold":{"views":["slip"],"outcomes":[{"actions":["blue"],"scores":{"exact":0}},{"actions":["gold"],"scores":{"exact":1}}]}
  },
  "requirements": [
    {"id":"blue-service","metric":"exact","mix":{"wants-blue":1},"at_least":"1/2"},
    {"id":"gold-service","metric":"exact","mix":{"wants-gold":1},"at_least":"1/2"}
  ]
}
```

Save that object as policy.json, then run `python3 -B local_policy.py policy.json` beside this guide. The result is feasible with margin zero. One local table picks blue and the other picks gold, each with probability 1/2. Both requirements are attained exactly. A client must obtain a shared draw independent of the commissioned scenario, install one complete table for all roles, and use only its own observation to act. This operation computes a distribution. It does not issue tickets or independently authenticate what each party saw.

Each of up to three roles declares view names and action menus. In each of up to 32 worlds, give the role views in role order and one scored outcome for every possible joint action in those views. Up to 24 requirements specify a score metric, positive weights on worlds normalized to a distribution, and exactly one inclusive lower or upper bound. The optional positive `scale` divides each signed slack so constraints with different units can be compared. Values can be signed integers, short fixed decimals or exact fraction strings. Distinguish presentation of a correct product, eligibility, exposure, and the actual cost of each purchase when choosing score units.

For at most 256 pure joint local tables the implementation maximizes the smallest expected normalized slack over shared mixtures. A nonnegative margin meets every clause. The returned plans give local maps and rational probabilities; achieved gives the original metric values. The optional clause prices form a normalized combination of the clauses bounding every eligible pure map and therefore every mixture in that class. A negative optimum quantifies the best attainable worst slack in the declared units. Scaling can change which compromise is chosen when clauses conflict. A resource-limit result carries no feasibility conclusion.

Use `"expose":[["desk","slip"]]` if that local entry is visible before the other side selects among the supplied requirements. The offer groups tables by the exposed action tuple. Every used public signal must satisfy the clauses conditionally; the best single group suffices for this stationary commission. The example then becomes infeasible: after either disclosed action, one of the two named states has zero service. Its conditional best margin is −1/2. The output gives a chosen signal and bounds for all groups. A noisy disclosure, an adaptive provider, cross-job correlation or unequal traffic after a signal requires its own receiving model. An action score is a supplied fact or assumption; compare it with the real delivery before relying on the policy.

### Find an experiment that distinguishes two command histories

Use `g60.history-inquiry.v1` when prior operations may have changed what a receiver will do next. Run `python3 -B history_inquiry.py experiment.json`. A first request can contain `{"commands":["stage","turn","take"],"traces":[]}`. Supply one to six distinct command names. The result `need-observations` lists command sequences. From comparable fresh starts, obtain one nonempty string return per command, then append records such as `{"calls":["take","take"],"returns":["empty","empty"]}` to `traces` and run the tool again. Every reported prefix also supplies an observation.

A `consistent-model` has an initial state, representative access histories and transitions mapping each command to its predicted return and next state. A new, independently observed counterexample can be added in the same format. A fitted model is a conjecture about the supplied observer. It has no global equivalence certificate or automatic authority to operate on a customer system. `inconsistent-replay` pinpoints mutually incompatible returns for one history. Check provisioning, return identity and reset conditions before continuing. `resource-limit` marks an inquiry beyond the conservative table, length or observation budget. The limits include 4,096 traces, 24 commands per trace, 256 expanded access rows, 32 distinguishing continuations and at most 64 requests per response. File inputs are trusted local JSON.

### Give a replacement an updatable record

Discover `g60.handoff-memory.v1` in the installed capability map. The reporter sees the initial `sender` observation. The replacement receives its encoded record, knows its own `receiver` observation when asked to act, and sees subsequent labeled events. Each named world supplies nonempty strings for `sender`, `receiver`, `action` and `public`. Each entry of `moves` is a triple of source world, event and target world. A JSON `null` event is invisible to the replacement and leaves its record unchanged. List silent moves explicitly. Each pair in `protect` requires equal records for the two named worlds in fixed placement and equal preexisting public observations. Supply `[]` when there are no such pairs.

For example, beside the installed program, save this as `case.json` and run `python3 -B handoff_memory.py case.json`.

```json
{
  "worlds": {
    "waiting": {"sender":"pending","receiver":"desk","action":"hold","public":"same"},
    "released": {"sender":"free","receiver":"desk","action":"admit","public":"same"}
  },
  "moves": [["waiting","accept","released"]],
  "protect": []
}
```

The minimum here is two symbols. Initially encode `pending`, and decode with `desk` to obtain `hold`. On `accept`, apply `update[record]["accept"]`; decoding that record yields `admit`. If an actual event is missing from the applicable table, the caller must obtain a new observation or hold the action. Neither event absence nor a missing decoder entry authorizes a default action.

With `placement: "fixed"` (the default), one sensor reading must always encode to the same record. The solver joins observations forced together by protected pairs, silent moves and indistinguishable labeled continuations, then searches for the smallest usable partition of up to eight remaining classes. A `continuation-collision` names the incompatible actions and gives the forced joins. Existing disclosure or immediate action conflicts can also produce `impossible`.

With `placement: "history"`, two arrivals at one physical world may carry different valid records. The encoder still depends only on the initial reading. The search covers up to five named worlds with possibly overlapping sets and tests whether each record has a compatible successor for each observed label. The `members` field describes each record’s possible worlds. A budget of 120,000 distinct search-node visits across palette sizes bounds that search. It can return `resource-limit` with a feasible upper bound. Protected pairs return `unsupported-scope` in history placement.

Both placements allow 1–128 named input worlds, at most 512 protected pairs and 256 listed moves, subject to their tighter search bounds. A `ready` result minimizes the number of record values in the selected model. The caller must supply the event model, choose private or public storage, preserve the record, verify any authority it relies on, and confirm actual products. Events or payouts visible to an outsider can distinguish complete histories even if a pair of retained records agrees. Direct access to current owner evidence can change how much the replacement needs to retain.

### Audit a changed receiving batch

Discover `g60.decision-audit.v1` and run `python3 -B decision_audit.py SPEC.json` beside the entry. Before any draw, freeze the epoch, receiving duty, binary mismatch measure, draw owner, named source frame, maximum receiving mismatch fraction (`limit`), total false-pass allowance (`risk`), inspection times and positive weights (`looks`), and warranted maximum removals, additions and changed outcomes through settlement (`change`). The audit requires a fixed underlying measurement for each source member and uniform sampling without replacement across the whole frame, including members who will refuse the read.

Save this example as SPEC.json.

```json
{"epoch":"dock-7","duty":"release-batch","measure":"wrong-file","draw_owner":"lottery",
 "frame":["A","B","C","D"],"limit":"1/4","risk":"1/2","looks":[[2,1],[3,1]],
 "change":{"basis":"fixed invoice","drop":0,"add":0,"flip":0},
 "evaluate":[{"match":4,"mismatch":0,"unread":0}]}
```

The first call returns `draw` and a plan digest. For the example, two original mismatches would make the billed frame unsafe. At the first look, two clean reads permit a pass. At the later look, the cumulative rule admits up to one bad-or-unread draw if no earlier stop occurred. At exactly two source mismatches the registered cumulative false-pass boundary probabilities are 1/6 and 1/2. The optional all-match scenario predicts release after two draws. Up to four `evaluate` scenarios on frames through 64 members can be submitted before the first draw. Each supplies three nonnegative integer counts summing to the frame size. Output `before_draw_performance` gives the four terminal-outcome probabilities and mean draws for each fixed composition under the uniform lottery. The scenario is a hypothetical input and does not change the frozen plan identifier. Remove it before submitting receipts.

For each distinct member selected by the lottery, append an object to `reads` with the returned `plan`, original `epoch`, selected `id` and `result` (`match`, `mismatch` or `unread`). Preserve the whole ordered prefix across calls. For example, two confirmed matches return `risk-pass` while `possible_rate` still spans zero to 1/2. Confirmed evidence can instead give `certain-pass` or `certain-fail` under the allowed cohort changes. If the last look cannot settle the decision, the program returns `inconclusive`. Unread selections remain adverse at the acceptance boundary, so a caller cannot buy another chance by discarding them. A mismatched plan, repeated unit or receipt after a terminal prefix is invalid.

The frame contains 1–256 distinct names. Use 1–32 increasing looks up to the frame size, each with an integer weight of 1–10,000. Removals must leave at least one original unit, flips cannot exceed frame size, and additions cannot exceed 256. Limit and risk are exact values between zero and one, with risk strictly below one. The plan includes the change basis. Establishing actual randomness, truthful change allowances, authentic receipts, sealed source measurements and single use across restarting clients are host duties. Direct complete inspection or shipping an unchanged sealed cohort can resolve a decision without statistical risk.

### Bound disagreement between two public histories

Discover `g60.coupling-bounds.v1`. Run `python3 -B coupling_bounds.py SPEC.json` beside the installed entry. Each of two trees has a named root and named nodes. Each node emits one public string. Its optional `next` maps each child name to a positive integer weight. The weights are normalized at that node. The model requires the left and right transition laws to hold even when conditioning on the **paired** past. Separate readings of two marginal populations do not establish that restriction. Evidence about a controller can warrant it; an anticipatory operator with information about the other side’s future may violate it.

Save this fair-stripe example as SPEC.json.

```json
{
  "left": {"root":"L","nodes":{
    "L":{"public":"ready","next":{"L0":1,"L1":1}},
    "L0":{"public":"light"},"L1":{"public":"dark"}}},
  "right": {"root":"R","nodes":{
    "R":{"public":"ready","next":{"R0":1,"R1":1}},
    "R0":{"public":"light"},"R1":{"public":"dark"}}},
  "conditions": []
}
```

The `bounded` result gives probabilities `0` and `1`. One returned plan puts the half-masses on the two equal pairs; the other puts them on the opposite pairs. Now suppose the custodian ensures both recipients use one immutable stripe for this epoch and verifies delivery to each endpoint. Under that premise replace the empty conditions list with the following value and run again.

```json
[
  {"at":["L","R"],"to":["L0","R1"],"upper":0,"basis":"verified shared stripe at both readers"},
  {"at":["L","R"],"to":["L1","R0"],"upper":0,"basis":"verified shared stripe at both readers"}
]
```

Both probabilities now equal `0`. Each step of each returned plan names `at` and lists nonzero triples of left child, right child and exact conditional mass. The plans include only pairs reachable under their own choices. An incompatible root returns `infeasible` and a list of blocked pairs. A blocked pair elsewhere can be bypassed only if its parent can allocate zero mass to it. A mismatch at an earlier public step counts even if the final strings become equal. An outsider seeing just one of the two streams poses a different classification problem; computing that observer’s best guess requires a prior and its observable distribution.

Each tree permits up to 48 nodes, four children per node, weights through 10,000 and a common terminal depth through eight. Use distinct children without sharing across tree branches. Up to 512 conditions give named paired parents in `at`, direct paired children in `to`, a nonempty `basis`, and a `lower`, `upper` or both. Use JSON integers or unsigned strings between zero and one. Strings may contain integers, a positive-denominator fraction or a fixed decimal, each with at most nine digits per part. Omitted endpoints default to zero and one. Repeated bounds intersect. Obtain authority, observations and changes to physical provisioning outside this calculation.

### Keep the operator's record private while publishing a reply

Discover `g60.private-reply.v1` and run `python3 -B private_reply.py SPEC.json`. For example, two senders can commission pending claims at different desks. The customer should wait in either case. After settlement the operator should tell the customer to collect. Both pending desks expose the same command menu and settlement reply. Save this as SPEC.json.

```json
{
  "worlds": {
    "a": {"sender":"desk-A","port":"service","receiver":"buyer","action":"wait","public":"counter"},
    "b": {"sender":"desk-B","port":"service","receiver":"buyer","action":"wait","public":"counter"},
    "c": {"sender":"settled","port":"service","receiver":"buyer","action":"collect","public":"counter"}
  },
  "moves": {
    "a-ack": ["a","settle","c","credit","ack"],
    "b-ack": ["b","settle","c","credit","ack"]
  },
  "protect": [[["a"],["b"]]],
  "max_records": 2
}
```

For each world supply the initial sender reading, publisher port, receiver input, required action and public observation. Each named move gives its source, command, destination, private update event and public event. A null private event leaves the record alone. A null public event emits no label for that move. Each protected path is an initial world followed by connected move IDs. The privacy comparison starts at the ends of the commissioning paths. A spectator who saw the commissioning process itself needs a separate check.

For a `ready` result, initialize the replacement with `initial[sender]`. On a visible private event apply `update[record][event]`. The publisher sends `report[record][port]`. The receiver performs `decoder[receiver][reply]`. A missing lookup calls for owner contact or a scoped hold. The compiled tables convey no permission to read a port or see a particular movement.

The comparison includes every common next command, the menu of available commands, the public world observation, each public move event and the reply at the resulting state. When several moves share a command it compares every cross-history pairing of those outcomes. A caller asserting a shared random cause or shared scheduler decision needs to establish that coupling at the input boundary. This deterministic comparison has no probability model. The private updater may hear events the public observer misses. An additional observer at the buyer's address, at the relay or on the billing feed may see a different transcript.

The objective minimizes reachable private record values first and the required reply alphabet second. An optional integer `max_symbols` from 1 through 12 caps the public alphabet. A tighter cap can require a larger private record or make the commission infeasible within the specified bounds. Inputs permit 1–8 worlds, at most four port values, 32 moves, 32 protected path pairs, up to 32 moves per path and a record bound of 1–3. The private search stops after 40,000 counted steps across its attempts. An environmental contradiction returns `impossible` with a distinguishing continuation. `no-design-within-bound` concerns the supplied maximum. `resource-limit` may include a feasible incumbent whose minimum is unresolved. Malformed input exits with status 2.

### Construct a reply for a receiver who has side information

Discover `g60.scoped-return.v1` via the capability map. Run `python3 -B scoped_return.py SPEC.json`. For example, try `{"worlds":{"w1":{"sender":"shelf-A","receiver":"map","action":"front","public":"quiet"},"w2":{"sender":"shelf-B","receiver":"map","action":"rear","public":"quiet"}},"protect":[]}`. Add `["w1","w2"]` to the protect list to require both alternatives to have identical public observations and reply.

On `ready`, the reporter looks up its observation in `encoder`. The receiver looks up its own observation, then that string symbol in `decoder`. Only modeled combinations have a promised action. `symbols` is the minimum alphabet size for a deterministic single reply on the supplied support. `already-visible` identifies a protected pair with differing existing public traces. `forced-collision` returns two conflicting action demands plus the protected pairs that joined the sender observations. An empty chain means the observations were identical. Inputs may contain 1–128 worlds and up to 512 protected pairs. Exact search accepts up to 12 remaining observation classes. A larger search returns `resource-limit`. Invalid input exits with status 2. Observer choice, factual completeness, acquisition rights, physical delivery and later disclosures require an external check.

### Choose a question that can settle this decision

The installed interface is `g60.probe-decision.v1`. Run `python3 -B probe_plan.py problem.json`. For example, an operator can ask whether a duplicate paper is acceptable before it releases an order.

```json
{"budget":2,"costs":{"duplicate-rehearsal":2},"models":{"one-only":{"answers":{"duplicate-rehearsal":"hold"},"action":"hold"},"one-or-more":{"answers":{"duplicate-rehearsal":"accept"},"action":"release"}},"observed":{}}
```

The result names the question and its minimum worst-case cost to reach agreement in this finite model. Obtain the actual answer from an authorized observer, put it in `observed` under that question, and update the remaining budget. Reissue the problem. The host supplies each candidate's predicted answer to every question and proposed action for the current order. You can keep the same predicted answers while changing the target action for a later order if the receiver's policy is still applicable.

The statuses are `action-agrees`, `ask`, `over-budget`, `inseparable` and `outside-models`. Agreement may conceal an omitted candidate. An inseparable decision needs a different source, an adequate direct route or a changed order. Positive integer costs, deterministic stable answers, at most 12 candidates and at most 32 questions define this exact search. The tool runs no offered operation and makes no external charge.

### Find who read a changed input

The installed contract is `g60.changed-reads.v1`. It takes two JSON files, the registered reads and the newly observed values.

Run `python3 -B read_routes.py receipts.json changes.json` with a named set of prior reads for each use and new observations for the keys being reported. For example, a receipt can contain `{"method-packet":{"source/selected":null}}` if that source was observed absent. A change then supplies the SHA-256 of the available bytes under the same name. The response maps each affected use to the precise keys that differ. An omitted input supplies no observation. When multiple readers use the same name their previous digests can differ.

The in-process form is `ReadRoutes(receipts).affected(changes)`. Retain the index across updates to visit the readers of the changed keys. Its `replace(use, reads)` method removes the superseded relationships and installs a new receipt. The caller supplies a trustworthy change feed, a stable namespace and any relevant code or selection reads. Explicitly enroll new uses. A nomination can trigger reconsideration. Any downstream effect needs the receiving owner's own authority.

### Compare observations held by different custodians

The installed contract is `g60.label-cuts.v1`. It takes the projected request JSON and the label required by this order.

Use `python3 -B compare_cuts.py request.json fern` when named custodians have already supplied authorized, ordered snapshots for one use. The request contains a nonempty `required` roster, a `scope` array, the string `code` being compared and `snapshots` keyed by holder. A completed snapshot names the same holder and scope, sets `complete` to true and `next` to null, and supplies its ordered `events`. Each event carries an `occurrence` pair [origin, ordinal], an `observation` identifier and a `printed` mapping from codes to labels. Assign origin and ordinal where the event occurs so a forwarded copy keeps its identity and an independently repeated reading gets its own.

Read `by_holder` and `gaps` alongside `status`. The result can be disagreement, an incomplete projection, another label, or a match at each holder's reported cut. A retransmission has no later effect at the same holder. The caller supplies trusted issuer identities, the authorized snapshots and the intended label. For work that depends on joint availability or actual output, obtain that separate return from the serving machines. The working archive contains a ready-made request in `evidence/independent-frontiers/divergence/pooled-request.json`.

### First encounter

Save the actual partial to `partial.txt`. A short `open.json` might read as follows.

```json
{"target":"Decide whether to release the next part","gap":"The notice may apply to a different assembly","prediction":"The reader holds this assembly","rival":"The reader releases it because the notice covers its predecessor","partial":"partial.txt","query":"notice superseded assembly"}
```

```sh
python -B turn.py /tmp/encounter.sqlite open open.json
```

Use the returned ID in `lookup.json` with `reader` pointing to the trusted release's `knowledge.py`. Select a candidate only if its complete body helps this decision. For a large owner, specify a window and retain its full basis. In `issue.json`, supply the prospect ID, selected source roles and paths, and each actual receiving decision and authority. Save issuance and observation IDs where the next investigator can recover them. The state file belongs to the operator. Share only the authorized packet or product, keeping future material and assessment details in custody. Use the adjacent independent-receiver host when disclosure conditions or role separation matter.

### What travels and what stays in custody

A plain path requests the complete file. To supply only a passage, use `{"path":"wide-field.md","start":0,"end":72}` with real zero-based UTF-8 byte offsets and an exclusive end. This works for a partial, source, authority or observation. The issuer stores the complete owner (up to 16 MiB with a window), exact range, owner and window digests. Only selected text travels. Treat a live wider field the same way, even when the entire field fits in the byte budget. `show ID` recovers the frozen record. `recover OWNER_SHA256 > original` recovers the exact complete owner including its original line endings. A passed hash alone cannot establish that the source is current or authorized.

Compact UTF-8 output, including its ID and newline, is limited to 32 KiB per ordinary command. Full-owner recovery is an explicit separate request. The bounds constrain transport. A brief paragraph can still restate many dimensions, reveal an answer or omit an important alternative. The operator screens the actual projection and checks the receiver's actual opportunity. Trusted local records establish byte identity and parentage inside the journal. Actual delivery, independence, external timing, semantic adequacy and any claimed decision effect require separate evidence. Independent record IDs do not prove independent observations.

For recurring searches, use native `knowledge.py prepare-index` and retain its returned digest. Pass `index` and `index_sha256` to `lookup` with the same `library`, `scan_limit` and `max_bytes` used in preparation. Ordinary lookup uses native BM25. Indexed lookup uses FTS5. Both operate on one selected native library, which supports at most 2,048 bodies. A larger source can be partitioned and routed externally. The native discovery answer may include wider coverage information for the custodian. Single-library lookup emits three nominations at most. Choice of corpus, route, query vocabulary and omitted scope still determine what can be found.

### Search several libraries without knowing the right shard

Supply the `lookup` action a host-selected manifest of prepared libraries, its SHA-256, the trusted native reader, and the prospect ID. The same frozen query reaches every declared shard. Each manifest row supplies its own scan and byte limits, index digest and stable shard name. A stale, missing or mismatched index fails the request. Recovery uses the winning shard, exact library digest and source identity. The caller can recover the original manifest and per-shard scope/cost audit with the returned digests. See [the federated lookup spec](INTERFACE.md).

Within each shard the native reader nominates up to the requested global limit. From these shortlists the merge prefers exact logical identity, then count of distinct matched query units, then local rank and manifest order. Shard-local BM25 scores are never compared. This deterministic rule can retain competing accounts, or omit one at the global cutoff. Native per-shard rank can also exclude material before the merge. Distinct pins and repeated language say nothing about independent evidence. Examine the complete owner in its jurisdiction before drawing conclusions.

Coverage counts sum the examined indexed records, omitted records and matched records across all declared shards. Counted matches can refer to overlapping real-world evidence. `matches_unissued` includes local and global cutoff losses. Native response bytes and verified index bytes describe different machine costs from the size of the reply exposed to the receiver. Corpus construction, the manifest and disjointness of real-world evidence remain operator responsibilities. Shards outside that manifest have unknown coverage. This design incurs one native invocation per declared shard. It saves receiver context without establishing a sublinear search cost or guaranteed recall.

### Checks

Run `G60_READER=/absolute/path/to/knowledge.py python -B -m unittest discover -s . -v` in this directory. The tests exercise the real native reader, reject a source with a changed nominated digest, withhold a 110-item inventory from every issued text channel in a first-paragraph window, retain full-owner recovery, and require a local return after transfer. The federation integration test searches three real prepared libraries containing 1,800 entries. It checks a final-shard exact witness, competing results, a vocabulary miss, global truncation and refusal of a modified index. Without the environment variable the two native integration tests are skipped.

### Find a prior investigation from a fresh job

Open a prospect for the new job in the same operator-owned journal. Its query can describe a material, symptom, decision or observed consequence even when the job has no old record ID. Run the recall companion with the **new** prospect ID.

```sh
python -B recall.py /tmp/encounter.sqlite NEW_PROSPECT_ID --limit 3
```

The first run builds a local SQLite FTS5 index over observed uses in this journal. It uses the first 24 distinct lexical query units, English stemming and BM25 to search intent, decision, issued partial, authority, selected source text, observed evidence and result. Subsequent runs index records appended since the last indexed row. The operation requires a trusted, writable journal and SQLite with FTS5. Broader or different vocabulary may be necessary when a useful observation is absent from the shortlist. Complete owners outside issued windows and records in other journals are outside this search.

Inspect the candidate's decision, marked lexical passage, original verdict, authority, exact evidence and source pins. The result shows recorded reopen/split branches, whether the partial matches, and the most recent later different verdict on that original issue/use when one exists. An absent flag supplies no current-authority assurance. Two other separately observed uses can appear when their records cite the **same issue and exact evidence owner/window**. Each still requires its own interpretation. This indexed join avoids constructing pairs across the archive. The reply reports stored observations, matches and unissued matches, at most five candidates and a 32 KiB ceiling. The index is a disposable derivative within the local journal. The read checks the canonical candidate and issue against their IDs.

Use `turn.py STATE show OBSERVATION_ID` for the complete record, then inspect the corresponding issue or recover a cited owner. If the finding could inform the new decision, pass that observation to `transfer` with the current use, warrant and challenge. Use `observe` after the new receiver returns. A future search can discover that new observation as well. The fresh-process regression demonstrates a material lookup, contradictory return, shared-evidence use, scoped transfer, zero-hit query and a previously unseen observation entering the index.

For a purchasing path that changes the right to act, supply an optional `custody` object such as `{"initial":"ready","accept":["ready"],"moves":{"ready":{"inspect":"spent","renew":"ready","sealed":"ready"},"spent":{"renew":"ready","sealed":"spent"}}}`. These offer names must occur in the commission's costs. Each purchase takes its declared public transition. An absent edge blocks that purchase. Both the local action checks and an accepting state are necessary to finish. A paid renewal may return the same observation in every installation. The caller supplies the warranted asset model, authenticates receipts, binds them to the current job, and exercises the actual rights. The companion performs pure routing from the supplied receipts. With the worst-cost objective, the returned policy also minimizes mean fee at the globally attained peak.

For the registered boundary-pricing entry, `--operation price` is the default. Use `--operation award` to price authorized observations and choices, `--operation recover` to select an exact disjoint recovery purchase, or `--operation retain` to buy a sufficient retained menu for an enforceable future tariff warrant. The latter takes an issued price message, one entry in `limits` per feasible row with `at`, `low`, `high`, and `keep` (retention fee, or null if unavailable), optional `differences` bounding the surcharge at `to` minus the surcharge at `from`, and an integer `tolerance`. The witness contains an attaining future invoice. Price preservation assumes all source alternatives remain authorized. Continuous origin rights, trusted source calculations and the availability of each owner's local award record require separate evidence.

In `format: facts`, owners supply separately named entities and predicates, local argument positions and observed tuples. The incidence adapter keeps predicate arity and port roles consistent across facts. A paid probe may name a local role such as `@slot/next/begin`. In `format: network`, peers have binary relative frames and each named issuer owns one or more comparison links. Set `request` to a peer pair. Qualification prices the minimum number of inconsistent issuers for each truth under the available records. Design prices offered links to protect that pair against a stated number of faulty and erased whole issuers. Several links from one office count as one fault domain. Authenticating cross-office independence and enforcing its availability are separate duties.

For a running job, `alignment_receipts.py issue SPEC JOB EPOCH` emits an observation desk carrying the job and commission, and `alignment_receipts.py advance DESK RECEIPTS` returns ask, act, reopen or hold. Receipts include job, epoch, commission, probe and value. The desk validates structural scope and schedule. Its caller authenticates the issuer, checks current epoch at exercise and commits any actual fee or product. A consistent owner lie cannot be rejected from its own assertion alone.
