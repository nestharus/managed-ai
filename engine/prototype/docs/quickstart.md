# Complete local example

Run from the extracted prototype folder on POSIX with Python 3.10 or later. The example writes **authored demonstration text** and records its actual file operations. It does not invoke or impersonate a model. Temporary work is outside the distribution and is left available for inspection.

```bash
set -eu
PYTHON="$(command -v python3)"
BASE="$(mktemp -d)"
export BASE
mkdir "$BASE/work" "$BASE/outputs"

"$PYTHON" - <<'PYSETUP'
import json, os
from pathlib import Path
base = Path(os.environ["BASE"])
work = base / "work"
(work / "brief.md").write_text("Construct a source-grounded note.\n", encoding="utf-8")
(work / "later-brief.md").write_text("Explain how the original instructions shaped the returned proposal. Treat the earlier context as historical material.\n", encoding="utf-8")
(work / "source.md").write_text("This source supplies a declared example fact, not a real-world observation.\n", encoding="utf-8")
(work / "field.md").write_text("Current use: produce the note. Other live work: examine a changed receiving use; obtain an actual external response where it matters.\n", encoding="utf-8")
scope = {"field": "field.md", "items": [{
    "id": "source-use",
    "question": "What does the source establish for this note?",
    "use": "Construct the current note",
    "basis": ["brief.md", "source.md"],
    "groups": ["writing", "source"]
}]}
(work / "scope.json").write_text(json.dumps(scope), encoding="utf-8")
(base / "outputs" / "reply.md").write_text("Caller-authored demonstration proposal; no model was invoked.\n", encoding="utf-8")
(base / "outputs" / "proposal.md").write_text("The supplied sentence is a declared example, not an independently observed fact.\n", encoding="utf-8")
PYSETUP

"$PYTHON" -B work.py --root "$BASE/exchange" init --workspace "$BASE/work"
"$PYTHON" -B attend.py --root "$BASE/exchange" track --spec scope.json
"$PYTHON" -B attend.py --root "$BASE/exchange" feedback first-look --text

"$PYTHON" -B work.py --root "$BASE/exchange" prepare first \
  --consumer writer --brief brief.md --material source.md --target answer.md
"$PYTHON" -B work.py --root "$BASE/exchange" receive first \
  --producer 'caller-authored documentation example; no model invoked' \
  --reply "$BASE/outputs/reply.md" --proposal "answer.md=$BASE/outputs/proposal.md"
"$PYTHON" -B work.py --root "$BASE/exchange" publish first \
  --target answer.md --reason 'Explicitly publish this authored local example.'

"$PYTHON" - <<'PYRECEIPT'
import json, os
from pathlib import Path
receipt = {"id": "account-1", "assessments": [{
    "item": "source-use",
    "disposition": "considered",
    "account": "The authored note distinguishes a declared example from an observed fact.",
    "remaining": ["No independent recipient has used this example."],
    "products": ["answer.md"]
}]}
(Path(os.environ["BASE"]) / "work" / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
PYRECEIPT

"$PYTHON" -B attend.py --root "$BASE/exchange" record first-look \
  --receipt receipt.json --producer 'caller-authored documentation example'
"$PYTHON" -B attend.py --root "$BASE/exchange" feedback after-work --text

"$PYTHON" -B work.py --root "$BASE/exchange" context first --text \
  > "$BASE/work/origin-context.md"
"$PYTHON" -B work.py --root "$BASE/exchange" prepare later-use \
  --consumer reviewer --brief later-brief.md --material origin-context.md --return first \
  --artifact first:answer.md --target review.md
"$PYTHON" -B attend.py --root "$BASE/exchange" inspect first-look --text
printf '\nRetained example workspace: %s\n' "$BASE"
```

The first feedback reports a nominated item with no public account. The later feedback reports a considered item with a named remaining question. The final packet receives the original producing context, reply and exact proposal body because its new brief asks about their relationship. It does not create a reviewer response or publish `review.md`. Another use can select only the reply or proposal when that is sufficient.

`answer.md` exists only because the caller explicitly published it. Attention receipt alone does not modify it. A captured product makes the public account inspectable, not automatically correct. Repeating the commands with the same immutable packet/view identities is not a fresh occurrence; use the retained workspace for inspection or start a new example directory.

For actual model work, replace the two authored response files with the actual recipient's outputs and attribute them accurately. Use [exchange.md](exchange.md) for command adapters and [attention.md](attention.md) for filtering, changing questions, deferral and source-object access. Do not treat this demonstration as a semantic experiment.
