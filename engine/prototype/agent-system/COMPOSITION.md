# Compose the system and its code

The application is one artifact in a larger working arrangement. Its code, operating instructions, proposed system, research account and actual receiving work can be composed and revised together. Start from the useful contribution and inspect the relationship that is supposed to produce it.

This guide supplies a concrete composition for this small application and a way to develop another. It does not prescribe a universal set of code dimensions, a compulsory decomposition or a task hierarchy. A direct explanation or known adequate operation can complete a bounded request.

## Predict a useful system before treating its parts as necessary

State what the proposed whole lets a recipient do. Project how the artifact, source, interpreter and later use would have to change for that consequence to occur. Work backward from the consequence to the contributions and observations that could supply it. A proposed gap remains a question until the missing connection is actually supplied or the projection is revised.

For example, a research-to-explanation system could let a maintainer produce a source-qualified explanation and a short narration from one claim. Its useful consequence is not “two tasks completed.” It is that both receivers get an explanation adequate for their uses, corrections reach the producers that need them, and unrelated work remains available while an optional source or media operation waits. That prediction may be satisfied by ordinary files and capable interpretation. It need not select a new agent platform.

Keep the original projection separate from its later interpretation. A review can contribute a counterexample, an alternative decomposition, a missing source or a corrected receiving question. The review itself is a composition: an actual question, selected source or proposed body, an interpreting participant, an account and a later consumer. A score or a reviewer label does not replace those relationships.

## The implemented responsibilities

| Component | Contribution | Connection to the rest of this system |
|---|---|---|
| [Operating guide](GUIDE.md) and actual capable participant | Semantic projection, question development, proposal construction and judgment | The selected guide enters each new context. The participant applies it to the particular artifact; the tool does not infer that application from file delivery. |
| `system.py`: request normalization and context composition | Selects records, sources, proposed artifacts and bounded derived views for an actual receiver | Calls the exact custody substrate and captures the producer/selection identities. Governing goal, field and obligation records remain visible. |
| `system.py`: record and whole-goal owners | Retains expected-revision changes and explicit judgments | Ordinary artifact content remains open. Current whole-goal use depends on the goal revision, evidence and separate obligations; local queue state is insufficient. |
| `composition.py` | Projects an explicitly supplied composition into relation observations and an open contribution field | Reads a nominated `g60-composition/1` record and its declared basis. It exposes supplied prerequisites, alternatives, missing qualification, interaction and omissions. It does not select the best work or execute it. |
| `information.py` | Preserves an optional declared information meaning through explicit change | Pins a defining body and preserves nominated content during wrapping or migration. An operation consuming that information still needs a compatible interpretation adapter. |
| `discovery.py` | Finds candidate source routes and checks selected source identities | Nominates records, accepted project sources and acquired catalogue bodies under explicit search bounds. The participant must still acquire and interpret the body for its current use. |
| Acquired Attention | Retains questions and public treatment for a source and receiving use | Supplied questions enter a selected context; later actual accounts record treatment. A new composition can require a new question even when earlier questions were considered. |
| Acquired Exchange | Preserves exact contexts, actual returns, proposed bodies and guarded publication | A returned proposal remains separate from its target. The owner checks the captured basis at acceptance; retained proposal re-entry supplies a current use after a consequential change. |
| Deployment entrance | Selects a complete accepted project and compatible producer for future calls | A mutable authoring tree, staged edition, selected edition and actual subsequent use have separate identities. Selecting an engine does not replace the stable controller or relocate runtime state. |

Several responsibilities belong in one Python process. Their separation here explains the relationships, not a requirement to turn them into separate services. The core and stable controller can remain unchanged while an app-level composition view is added.

For a broad first reading, the interaction map factors named overlaps by resource. One shared destination can yield one group even when many writers use it. If the decision needs a positive witness, inspect that group and contact the actual owner. If it needs to know whether a particular pair is independent, check the original footprint, physical binding, operation, and receiving use. The number of unknown footprints does not identify which opportunities have them.

## Use an explicit composition where it helps the current work

An ordinary record can carry an optional `g60-composition/1` view. It names the intended contribution, relevant elements and relations, supplied premises, possible contributions and known unknowns. The underlying artifacts need not be JSON, and these fields do not define what every artifact is. They define what this particular view can calculate.

This projection reads ordinary `record.content` with that composition format. It refuses an information-envelope wrapper rather than silently unwrapping its meaning. Keep the composition record ordinary for this entrance. A future wrapped-content adapter would need to preserve the explicit interpretation supplied by that wrapper; the existence of both optional mechanisms does not make their composition automatic.

The [command reference](README.md) owns the complete field contract. The public entrance is:

```bash
python3 -B system.py --root ../my-ai-work compose --record explanation-system
python3 -B system.py --root ../my-ai-work compose --record explanation-system \
  --focus relation:claim-to-explanation --limit 6 --pair-limit 100
```

For a selected deployment, use its stable `deploy.py --deployment ... system` prefix. A view can also enter a normal context request:

```json
{
  "task": "Develop a source-qualified explanation and identify any relation that this proposed system fails to supply.",
  "consumer": "actual explanation contributor",
  "compositions": [
    {
      "record": "explanation-system",
      "focus": "relation:claim-to-explanation",
      "limit": 6,
      "pair_limit": 100
    }
  ],
  "sources": ["sources/claim.md"],
  "targets": ["outputs/explanation.md"]
}
```

Use actual record identities and existing source paths. A composition view cannot make an unavailable source present. Select the complete source or proposal bodies the receiver needs, with exact source pins when carrying a discovery basis. The view's declared counts describe its supplied scope, not every useful contribution in the world.

An opportunity's `at` references locate a possible contribution at elements or relations. Its `requires` alternatives are an OR of AND groups: one fully available group can discharge the declared prerequisites. An empty list declares no prerequisite; it does not prove the opportunity useful or universally feasible. Premise availability is supplied meaning, qualified by its declared basis. A source marked unavailable or unassessed does not become available because a parent artifact is useful for a different purpose.

Keep readiness attached to the particular receiving contribution. A script can be ready for conceptual review and still lack what final narration requires. A blocked rendering route does not necessarily block an explanation, a source critique or the prediction of another system. A parent-child relation, similarity or proximity is not a prerequisite unless the author supplies that meaning.

Focus locates a working region while the view retains wider counts and omitted references. A returned candidate is not a schedule. Inspect nominated reads, writes and explicit conflicts before selecting parallel contributions. An undeclared footprint remains unknown; separate filenames alone do not establish independent effects. Sharing a source can be harmless when both contributions only read it, while changing that source can affect the other contribution's basis.

## Turn source claims into useful inspection

Take a concrete claim from the research and follow it into a proposed producer and a later receiving consequence. Ask what observation could distinguish the adequate connection from a missing or unnecessary constraint. If the projection breaks, locate whether the gap belongs to the proposed artifact, the implementation, the source explanation, the available participant or the receiving question.

An informative observation has a producer as well as a delivered format. For example, take a source that independently accepts each attempted candidate with its fixed positive probability. A producer that repeats one fixed proposal distribution until acceptance returns a normalized distribution of successful candidates. Separate accepted reports can leave a relative rate unresolved; putting the relevant candidates in the same proposal before acceptance can reveal it. Mixing the already completed reports does not create that common acceptance relationship, and the fixed-proposal family can still hide an absolute scale.

Compare a different permitted producer: try the target once; on rejection, switch to a distinct fallback until acceptance. The probability that the final identity is the target is now its per-attempt acceptance probability. The producer supplies rejection feedback and memory of the switch; the recipient needs distinguishable terminal identities and the correct interpretation of this sequence. The output schema alone does not distinguish these two observing arrangements. Almost-sure fallback completion does not establish a fixed time bound, and selecting only runs finished before a cutoff can change the source seen by the recipient. A direct source answer or available raw outcome may already serve the question.

The capable participant supplies the source meanings, allowed operations and inference. The app can capture that construction, deliver its exact bodies and retain the actual return; it does not derive identification or design the changed producer automatically. Follow the source event through the proposed sequence into the receiving question before deciding whether the useful change belongs to the producer, its exposure or its consumer.

For this app, “reopen the field after a consequential return” interacts with how contexts and publication are composed. The field is a governing record delivered to every context. Exchange checks every declared material and target before publication. Revising the field can therefore invalidate an outstanding context's publication basis even when its proposed body remains useful. The guard is doing its declared work; the continuing arrangement needs a current interpretation of that return.

Do not erase that history or silently ignore the changed premise. Select the retained body through a new request's `artifacts`, such as `"earlier-turn:outputs/explanation.md"`, and select the relevant current sources. The actual receiver can decide that the old body remains adequate, revise it or choose another route. Receive the current contribution and adopt it through the owner. The new use does not require manufacturing a textual change merely to demonstrate continuation. A material change to a governing goal, source or target still needs an actual qualified judgment.

The same inspection can reveal a gap in the source research. A prose instruction may describe a valuable relationship without explaining how an agent constructs its operative question in this receiving situation. Repair the affected explanation or producer and make the changed body reach the next actual use. Adding a generic warning beside unchanged production leaves the productive connection unresolved.

## Construct a reviewer for an existing artifact

A bounded reviewing system can reuse this app around a capable participant. Select the useful effect first: for example, determine whether an existing executable example supplies its promised source ownership, personal work and explicit publication relationships, then carry warranted corrections into the responsible producers. An adequate result is also useful; a defect is not required. Direct capable-agent work over ordinary files remains an adequate alternative.

A separate native store is useful when the reviewing duty needs its own goal and retained work. Initialize it from the actual selected goal and select the domains and source catalogues that provide the accepted project sources. Create a deployment that points to the common engine and this store through the [shared-engine and store contract](../../INSTANCES.md). Source access belongs to the store's selected catalogues; each request selects the complete bodies needed for its use. When changing the selected engine, stage a compatible engine reference and activate it with the required candidate identity and expected descriptor guards. Staging retains selection metadata while the code and store keep their existing owners. Preserve any partial construction or uncertain effect instead of automatically repeating it. These existing operations can be composed for the reviewing duty.

For a review intended to develop missing questions, first select the target's stated effects, contracts and relevant source argument. Ask the participant to retain a short public prediction before acquiring implementation bodies: which productive relationships matter to the recipient, what would distinguish a failure from adequate behavior, and which sources could answer that question. Then acquire the needed complete bodies through an actual source selection. Known paths can be used directly; omitted inventories do not establish reading. The ordering makes the question's origin inspectable without making it compulsory for every review.

Let an actual trace revise that prediction. An owner can check destinations correctly while a preceding adapter silently substitutes one; a receipt handler can preserve later typing while an earlier refresh already erased it. The observer therefore follows the whole passage that supplies the receiving effect. Return the particular source basis, actual observation or adequate source account, responsible producer, and complete proposed correction where warranted. Preserve the original failure beside the changed producer's later use and retain useful behavior that needs no repair. A prompted follow-up, such as guidance for old requests after an interface change, keeps its supplied origin.

Keep useful sibling work available while one connection is repaired. Code proposals can proceed separately while shared contract pages still require one composed replacement. The owner receives and accepts justified contributions, obtains the subsequent use needed by the original prediction, and reopens the whole duty. The app retains identities, selected bodies, proposals and actual returns. It does not derive the semantic finding, certify alignment or establish an accuracy or cost advantage over the direct route. Adequate acquired research and application support can be retained when the missing relationship belongs to the target implementation.

## Make a code change as a contribution to this artifact

Before changing a component, record the actual relationship the change is meant to supply and the current implementation it depends on. For the optional composition view, the selected arrangement is a read-only projection module plus small CLI/context integration in `System`, using existing records, basis checks and custody. This keeps the new convention inspectable without making every existing record conform to it.

The serious direct alternative is an ordinary composition document with existing records, Attention and proposal re-entry. Prefer that route when it already serves the use. An import graph can expose structural coupling but does not supply intended meaning. A broad refactor is justified by a concrete responsibility or interface problem, not by a desire for more abstract boxes.

Construct the smallest complete contribution, then inspect the interactions its own use creates. Useful cases include a ready sibling beside a blocked realization, two alternative ways to satisfy one need, a cycle with no supplied starting contribution, unknown effect overlap, a changed declared source and a bounded view that omits part of the supplied composition. These cases examine the selected contract; they do not establish that every research implication has been represented.

Keep the proposed source, actual observations and resulting design interpretation available together. A code review can change the decomposition, the question or a different producer. An accepted patch contributes when the next selected engine actually uses it; a successful isolated check and a changed authoring file establish narrower things.

## Reopen the containing construction

After a consequential return, compare the immediate continuation with a concrete sibling, alternative or broader contribution. Recompute a useful view when the declared composition or basis changes, but also ask whether the view itself omitted a consequential relation. A complete inventory of supplied rows cannot answer that semantic question.

An execution request is a way to obtain a contribution. It need not become the primary representation of the whole project. Tasks, schedules, code, reviews, diagrams and videos can all be ordinary artifacts when their receiving relationships make them useful. The selected composition can generate particular work requests and later retire them when another arrangement supplies the outcome.

A proposed successor system is likewise an artifact. To investigate its adequacy, use the current system to construct it, give it to an actual receiver, and obtain the output or changed action the prediction claimed. Preserve where the successor failed and whether the missing relation belonged to the parent support or the research itself. Repeat when that return selects a consequential improvement. A system description, an executed fixture, actual receiving use and broad general success remain different claims.
