# A small inventory convention

Use the existing inventory where it already supplies these distinctions. Add only the missing receiving relationships. This convention describes ordinary artifact bodies; it does not change the collective store format or introduce another runtime schema.

## Separate records that have different meanings

| Record or field | Meaning | Update rule |
|---|---|---|
| Report occurrence | One received report, its useful body and producing source/question | Preserve its identity; exact delivery retry refers to the same occurrence |
| Consolidation scope | The current purpose, selected reports, groups, open distinctions and route intents | Publish as one revisioned target under its owner's current basis |
| Group | A selected relationship among reports for a declared purpose | May merge, split, overlap or become contested; keep predecessor/support relations where useful |
| Work contribution | A requested artifact or action and its current receiving conditions | Retain useful output even when its old acceptance basis becomes stale |
| Route intent | A request to supply a contribution to a recipient | Stable intent identity; attach the actual accepted scope pin when dispatching |
| Effect record | A particular authorized effect and its observed outcome | Keep at the effect owner; regrouping alone does not create a second effect |
| Advisory session state | Known child handles, live integration attempt, proposed next window | Reconstruct or reconcile after interruption; it does not replace authoritative acceptance |
| Continuing service, when promised | Stable duty identity and original waiting/deadline/credit relation, remaining demand and accepted destination commitment | Context replacement preserves the duty; moving work re-admits the residual promise rather than silently resetting arrival |

The authoritative target revision and body digest come from the store. Do not maintain a second manually incremented “current revision” inside the same body. Capture actual source pins through the host's reading operation. The included example uses readable artifact IDs only and is deliberately not an executable captured context.

## Suggested scope body

The example uses these few fields:

- `purpose`: what grouping should let a receiver do.
- `included_reports`: resolvable original report IDs.
- `coverage`: what this selected view includes and leaves open.
- `groups`: purpose-qualified membership, interpretation and current disposition.
- `open_questions`: distinctions that the next work may need.
- `route_intents`: the requested receiving contribution and its identity.

These names are an example, not a universal domain ontology. A narrative canon, research claim view or branch plan can use different fields while preserving the same relationships. The store does not judge the semantic adequacy of their values.

## One current publication

An integration worker obtains a current scope capture, reads its selected sources and required findings, and returns a complete proposed body. `propose` retains that actual return. The owner then publishes with its account, current claim token where applicable, and the required treatments. The accepted scope body contains its grouping and route intents together.

A dispatcher combines an accepted intent with the **actual enclosing publication pin** returned by the owner. The body need not predict its own future digest. The receiver captures that scope or another warranted current view before an effect whose correctness depends on it. A dispatch receipt establishes delivery under the host's contract; the downstream owner separately records its effect.

Treating a required finding means that its receiving interpretation was supplied at that target. It does not silently mark a repair, CI job, branch merge or source obligation complete. Close those duties only with the evidence and acceptance their actual owners require.

## A successor continues the original duty

Keep useful progress, consumed service and remaining demand separate. A short handoff may reduce source reading or require additional reconstruction. Re-estimate the new route without silently changing an original deadline or replacing an uncertain attempt. If the existing owner can retain its queue and effect custody, replacing only the context is sufficient.

When work actually moves, the destination must be able to honor the residual promise under its policy and capacity, or expose the change for the duty owner. Raw priority values from different queues need not be comparable. An acknowledged packet and a free execution slot do not establish this guarantee. Best effort remains valid when that is the actual promise.

## Later merge or split

Retain report IDs. Give new group interpretations meaningful identities or predecessor links where later work needs them. Preserve an intended action's identity when only the display grouping changes. When the action itself changes, create a new action identity and relate it to the superseded plan.

Re-evaluate dependent work under the new interpretation. Keep an adequate subartifact. Refuse or revise an old blanket action whose captured scope is stale. An unrelated scope remains available when its own relevant conditions remain adequate.

An already completed effect remains an observed historical occurrence. A later split does not erase or repeat it. Where correction is needed, select the actual corrective effect and its authorization. An uncertain outcome must be reconciled with its owner before deciding what remains to do.

## Absence, completeness and cross-scope constraints

`included_reports` states what the view includes. It does not mean “all reports that could be relevant.” An inbox cursor is a transport position, not a semantic completeness mark. A closed-generation claim needs an actual rule establishing membership and the treatment of later arrivals.

If admission depends on an absence such as “no competing work is currently authorized,” place that condition at an owner that can check it within the required acceptance operation. A versioned scope/membership artifact can supply a guard when it actually owns the relevant changes. A required-offer inbox makes admitted offers consequential; it does not discover unoffered relevance. Capturing two existing row versions alone does not guard a missing third row or an unrepresented cross-scope exclusion.

The minimal pattern keeps one consolidation scope in one target. The current collective adapter does not atomically publish several targets. If a receiving promise truly requires that stronger operation, use an adequate joint target, keep the changes provisional, or supply an actual stronger admission mechanism. Do not infer it from the JSON shape.

## Complete illustrative body

This is explanatory data before the provenance investigation, not an executed study or an API request. Adapt the individual artifact bodies through actual host operations. Source pins and accepted revisions come from their returned receipts.

```json
{
  "illustration": {
    "status": "explanatory-data-not-an-executed-study",
    "purpose": "Show ordinary report and consolidation-scope bodies before a title-provenance investigation.",
    "usage": "Adapt the bodies through the host's real operations. Obtain source pins and publication revisions from actual returned receipts."
  },
  "report_artifacts": [
    {
      "id": "report-summary-title",
      "body": {
        "report": "The workspace summary title remains in the original language after the display locale changes.",
        "producing_question": "Which user-facing text should follow the display locale?",
        "unresolved": "Whether this title is user-authored literal content or application-authored text."
      }
    },
    {
      "id": "report-export-heading",
      "body": {
        "report": "The exported report heading remains in the original language after the display locale changes.",
        "producing_question": "Which user-facing text should follow the display locale?",
        "unresolved": "Whether this heading shares the summary title's source and required behavior."
      }
    }
  ],
  "consolidation_scope_artifact": {
    "id": "scope-title-investigation",
    "body": {
      "purpose": "Select useful investigation and repair work for the received title reports.",
      "included_reports": [
        "report-summary-title",
        "report-export-heading"
      ],
      "coverage": {
        "included": "The two named received reports and their unresolved provenance question.",
        "outside": "Other text paths and unreceived reports.",
        "closure": "This is a selected view, not a closed complete obligation generation."
      },
      "groups": [
        {
          "id": "group-title-provenance",
          "members": [
            "report-summary-title",
            "report-export-heading"
          ],
          "meaning": "One shared investigation of title provenance and intended behavior.",
          "disposition": "provisional",
          "remaining_distinction": "The reports may require separate repairs once their source meanings are known."
        }
      ],
      "open_questions": [
        "Which text is user-authored literal content?",
        "Which text is application-authored and should follow locale?",
        "Can one result fulfill both report-specific duties?"
      ],
      "route_intents": [
        {
          "id": "route-investigate-title-provenance",
          "recipient_role": "root-build",
          "requested_contribution": "Inspect the actual title sources and produce a source-qualified behavior account before selecting a repair.",
          "report_duties": [
            "report-summary-title",
            "report-export-heading"
          ],
          "acceptance_condition": "The receiver uses the actual accepted scope basis and preserves both report-specific questions.",
          "effect_status": "No downstream effect or completed delivery is represented by this intent."
        }
      ]
    }
  },
  "independent_scope_artifact": {
    "id": "scope-keyboard-focus",
    "body": {
      "purpose": "Prepare a keyboard-focus correction under its own selected source and receiving conditions.",
      "included_reports": [],
      "coverage": {
        "included": "Only the current focus contribution selected by its owner.",
        "outside": "Title-provenance decisions unless a consequential shared relationship is discovered."
      },
      "groups": [],
      "open_questions": [],
      "route_intents": []
    }
  }
}
```
