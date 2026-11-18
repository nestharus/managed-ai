# Bounded callable agent runtime

`agent_runtime.py` provides a small, synchronous Python runtime using only the
standard library:

```python
run_agent(*, task, sources, model, tools, max_steps) -> dict
```

Supply the model and tools as Python callables. The runtime calls the model at
most `max_steps` times, dispatches at most one tool per model action, and returns
an ordered trace with detached snapshots of the interaction.

## Example

Keep `agent_runtime.py` and `evidence_support.py` together on the Python import
path, then run:

```python
from agent_runtime import run_agent


def model(request):
    observations = [
        event for event in request["history"]
        if event["kind"] == "tool_return"
    ]
    if not observations:
        return {
            "kind": "tool",
            "name": "multiply",
            "arguments": {"left": 7, "right": 3},
        }
    product = observations[-1]["value"]["product"]
    return {"kind": "final", "answer": f"The product is {product}."}


def multiply(arguments):
    return {"product": arguments["left"] * arguments["right"]}


result = run_agent(
    task="Multiply 7 by 3 using the supplied tool.",
    sources={"instructions": "Return the observed product."},
    model=model,
    tools={"multiply": multiply},
    max_steps=2,
)

assert result["status"] == "complete"
assert result["answer"] == "The product is 21."
assert result["model_calls"] == 2
assert result["tool_calls"] == 1
```

The example model is a deterministic callback. A supplied callback can perform
its own model inference; this component does not include a provider client.

## Inputs and model requests

`task` is a string. `sources` maps string IDs to complete string bodies. `model`
is a callable accepting one request dictionary. `tools` maps string names to
callables, each accepting one arguments dictionary. `max_steps` is a nonnegative
integer counting attempted model invocations, including an invocation that
raises an exception.

Each model call receives:

```python
{
    "task": original_task,
    "sources": {source_id: original_body},
    "step": one_based_call_index,
    "remaining_steps": max_steps - one_based_call_index,
    "history": [...],
}
```

History contains prior model returns, tool calls, tool returns and callback-error
events in interaction order. It excludes model-request events so earlier full
requests are not nested recursively into later requests. Mutating a delivered
request or tool arguments cannot rewrite retained evidence. Source bodies and
their SHA-256 digests are captured at entry, using their exact UTF-8 bytes without
whitespace or Unicode normalization.

The model returns one of two actions:

```python
{"kind": "final", "answer": "Exact answer text"}
{"kind": "tool", "name": "registered_name", "arguments": {}}
```

Required fields and types are checked before tool dispatch. Extra fields may be
retained and ignored. Malformed actions and unknown tool names terminate the run
without retries or substitution. Tools return observations that are retained in
the trace and delivered in the next model request's history. Finite
JSON-compatible callback returns are preserved completely, including malformed
model actions. Non-JSON objects receive type-level diagnostics rather than a
claim that their string representation is an exact copy.

## Results and stopping

The result is a JSON-compatible dictionary with `status`, `answer`,
`model_calls`, `tool_calls`, `error`, and `trace`.

| Status | Meaning |
| --- | --- |
| `complete` | The model returned a valid final answer. |
| `step_limit` | No final answer was returned within the model-call budget. |
| `malformed_model_return` | The model action has an unknown kind or missing/wrongly typed required fields. |
| `unknown_tool` | A valid tool action names a tool absent from the supplied registry. |
| `model_error` | An attempted model callback raised an exception. |
| `tool_error` | An attempted tool callback raised an exception. |

`answer` is the exact model answer only for `complete`; otherwise it is `None`.
`error` is `None` for `complete` and `step_limit`, and a structured diagnostic for
error statuses. Counters include callbacks that raise. No callback follows a
terminal error.

A final answer on the last allowed model call completes successfully. A valid
tool action on that call still executes and its observation is retained, then
the run ends with `step_limit`. A zero budget invokes no callbacks. Empty source
and tool mappings are allowed.

The trace contains the original task, entry source bodies and SHA-256 digests,
the model-call budget, and ordered event dictionaries. Events record model
requests, model returns, tool calls, tool returns and callback exceptions. The
request event preserves the request as delivered, before the callback can mutate
it. Return values and tool arguments are likewise detached at their boundaries.
Exception events include the callback kind/name, exception type and message.

## Scope

The budget limits callback counts. It does not impose a timeout, sandbox callback
code, limit provider cost, undo external side effects, or verify whether a final
answer is correct or supported by the supplied sources. Callback code runs
synchronously in the caller's process.
