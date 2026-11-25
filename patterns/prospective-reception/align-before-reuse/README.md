# Decide which part of a working design to reuse

The dispatch desk asks how many cartons it needs. An installed utility already answers that question for an order of interchangeable, single-slot items. The tempting extension is to run the utility once per submitted batch and add its answers. Before adding that loop, ask what the receiving desk must actually hand over. A work submission and a sealed delivery can have different boundaries.

The starting utility is the actual carton program generated and accepted in G60 v30’s offline receiving example. The replay installs it from that producer and runs it as a subprocess. The new commission and recipients are fixtures. Each carton is a real ZIP containing the original item payloads. A receiving program opens it and checks the allowance, its destination, contents and capacity. Its accepted items and outstanding deliveries are the outcomes.

## Try the inherited boundary

The manifest contains three plates. One submission arrangement brings them together. Another brings one plate first and two together later. Both arrangements contain identical item identities and contents. A carton holds three single-slot plates.

| Commission | Applying the counter at each submission | Applying it at each delivery |
|---|---|---|
| All three plates go to the conservation desk. One carton is available. | The combined submission delivers all three. Splitting the submissions asks for two cartons and leaves two plates outstanding. | One carton reaches conservation with all three plates. |
| North must receive one plate separately from South’s two. Two cartons are available. | One combined submission proposes a single sealed carton. It crosses the receiving commitments and the receiver refuses it. The split submissions happen to succeed. | North and South each obtain a carton with their own plates. |
| North’s delivery is established. The other two destinations are unknown. Three cartons are available. | The submission boundary supplies no evidence of their destinations. | North receives its plate. The other two remain owed pending a destination. |

**Decision.** Keep the scalar counter for an established compatible group. Split submission intake from the formation of delivery groups. A direct single order already works. The desk can also act on a known independent destination while another remains uncertain, given the stated spare capacity. This example supplies the receiving contracts; real adoption must obtain them from the relevant participants. The original counter fulfilled its own tested commission.

## Challenge the replacement

Now every plate occupies two slots and must stay whole. The three plates still go to one destination. Counting items calls for one carton. Feeding the six occupied slots into the same counter calls for two. There are eight ways to assign three distinct plates to two cartons, and all eight exceed the capacity of a carton. The receiver accepted an actual three-carton packing. If only two cartons were available, another run of the scalar counter could not supply this transfer.

For this receiving promise the counter can give a lower bound from total slots. An exact promise needs a feasible packing. The simple grouping correction was sufficient only while items were interchangeable single-slot units. No general packing solver is needed for the direct or pooled controls.

## Find the relevant source while deciding

The investigator's original question was “Can we reuse our existing carton counter when combining orders submitted in separate batches?” This is a new query for this example. V30 examined 80 complete bodies mechanically, matched 73 and displayed six. Coordination boundaries came first. Receiving boundaries was absent from the six. The fourth result, the current architecture, directly referenced it. The replay acquires and verifies all three complete bodies. The receiving-boundary source supplied a reason to inspect which recipient could accept the proposed carton, leading to the admission test. None of the source bodies supplied the recipients’ routing contract. The external review had already called attention to application batching, so this is an informed transfer.

Keep the original receiving question, why the source seemed relevant, and the operation its acquisition actually changed. A missing top-six result is an occasion to follow a reference or ask who owns the missing relationship. It does not warrant making every inquiry read eighty documents.

## Replay

```sh
python3 -B alignment.py /path/to/G60-system-use
```

The argument is the root of the extracted operating compact, including the v30 offline example. Python 3.11+ suffices. The result includes the producer's executable digest, source-manifest digest, reader coverage and pins, actual per-destination contents, refusals and remaining items. The ZIPs model discrete items and acquisition limits. The exercise establishes a conditional construction under those rules; it does not measure a warehouse, automatic semantic discovery or a person's understanding. Discovery ranks can change with a newer catalogue.
