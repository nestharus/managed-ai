"""Real-boundary unit tests for companions/workbench/{agent_runtime,evidence_support}.py."""

import hashlib
import math
import sys
import unittest
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai_information.companions.workbench.agent_runtime import run_agent  # noqa: E402
from managed_ai_information.companions.workbench.evidence_support import (  # noqa: E402
    JsonSnapshotError,
    capture_return,
    detached_json,
    interaction_history,
    source_snapshots,
)


class _ExplodingRepr:
    def __repr__(self) -> str:
        raise AssertionError("__repr__ must never be called by capture_return")

    def __str__(self) -> str:
        raise AssertionError("__str__ must never be called by capture_return")


class EvidenceSupportTests(unittest.TestCase):
    def test_detached_json_copies_without_aliasing_and_rejects_invalid_values(self) -> None:
        shared = [1, {"k": True, "n": None, "f": 2.5}]
        payload = {"first": shared, "second": shared}
        snap = detached_json(payload)
        self.assertEqual(snap, payload)
        self.assertIsNot(snap["first"], shared)
        self.assertIsNot(snap["first"], snap["second"])

        shared.append("mutated")
        self.assertEqual(len(snap["first"]), 2)

        for bad_val, pattern in (
            (float("nan"), "nonfinite float"),
            (math.inf, "nonfinite float"),
            (-math.inf, "nonfinite float"),
            ((1, 2), "unsupported type"),
            ({1: "bad-key"}, "object key is not a string"),
            ({"nested": _ExplodingRepr()}, "unsupported type"),
        ):
            with (
                self.subTest(bad_val=type(bad_val).__name__),
                self.assertRaisesRegex(JsonSnapshotError, pattern),
            ):
                detached_json(bad_val)

        cyclic: list[Any] = []
        cyclic.append(cyclic)
        with self.assertRaisesRegex(JsonSnapshotError, "cyclic container"):
            detached_json(cyclic)

    def test_capture_return_isolates_valid_and_non_json_values_without_repr(self) -> None:
        val = {"kind": "final", "answer": "ok"}
        snap, ok = capture_return(val)
        self.assertTrue(ok)
        self.assertEqual(snap, val)
        val["answer"] = "mutated"
        self.assertEqual(snap["answer"], "ok")

        diag, ok_bad = capture_return(_ExplodingRepr())
        self.assertFalse(ok_bad)
        self.assertIn("non_json_value", diag)
        self.assertTrue(diag["non_json_value"]["type"].endswith("_ExplodingRepr"))
        self.assertIn("unsupported type", diag["non_json_value"]["reason"])

    def test_source_snapshots_hashes_exact_utf8_bytes_and_validates_types(self) -> None:
        raw_text = "  Line1\r\nCafé \n"
        bodies, records = source_snapshots({"doc-1": raw_text})
        self.assertEqual(bodies, {"doc-1": raw_text})
        self.assertEqual(records["doc-1"]["body"], raw_text)
        self.assertEqual(
            records["doc-1"]["sha256"], hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        )
        with self.assertRaises(TypeError):
            source_snapshots(cast(Any, ["not-a-mapping"]))
        with self.assertRaises(TypeError):
            source_snapshots(cast(Any, {1: "body"}))
        with self.assertRaises(TypeError):
            source_snapshots(cast(Any, {"doc": 123}))

    def test_interaction_history_filters_model_requests_and_detaches(self) -> None:
        events = [
            {"kind": "model_request", "step": 1, "request": {"secret": "omit"}},
            {"kind": "model_return", "step": 1, "value": {"kind": "tool", "name": "lookup"}},
            {"kind": "tool_call", "step": 1, "name": "lookup", "arguments": {"q": "a"}},
            {"kind": "tool_return", "step": 1, "name": "lookup", "value": {"hits": [1]}},
        ]
        hist = interaction_history(events)
        self.assertEqual([e["kind"] for e in hist], ["model_return", "tool_call", "tool_return"])
        hist[2]["value"]["hits"].append(2)
        self.assertEqual(events[3]["value"]["hits"], [1])


class AgentRuntimeTests(unittest.TestCase):
    def test_input_validation_fails_before_invoking_model_or_tools(self) -> None:
        called = {"model": 0, "tool": 0}

        def model(_req: dict[str, Any]) -> dict[str, Any]:
            called["model"] += 1
            return {"kind": "final", "answer": "never"}

        def tool(_args: dict[str, Any]) -> dict[str, Any]:
            called["tool"] += 1
            return {}

        for kwargs, exc_type in (
            ({"task": 1, "sources": {}, "model": model, "tools": {}, "max_steps": 1}, TypeError),
            ({"task": "t", "sources": [], "model": model, "tools": {}, "max_steps": 1}, TypeError),
            (
                {"task": "t", "sources": {"s": 1}, "model": model, "tools": {}, "max_steps": 1},
                TypeError,
            ),
            ({"task": "t", "sources": {}, "model": None, "tools": {}, "max_steps": 1}, TypeError),
            ({"task": "t", "sources": {}, "model": model, "tools": [], "max_steps": 1}, TypeError),
            (
                {"task": "t", "sources": {}, "model": model, "tools": {"x": 1}, "max_steps": 1},
                TypeError,
            ),
            (
                {"task": "t", "sources": {}, "model": model, "tools": {}, "max_steps": True},
                TypeError,
            ),
            (
                {"task": "t", "sources": {}, "model": model, "tools": {}, "max_steps": -1},
                ValueError,
            ),
        ):
            with (
                self.subTest(exc_type=exc_type.__name__, kwargs=list(kwargs.values())[:2]),
                self.assertRaises(exc_type),
            ):
                cast(Any, run_agent)(**kwargs)

        self.assertEqual(called, {"model": 0, "tool": 0})

    def test_multi_step_tool_roundtrip_detaches_callbacks_and_handles_non_json_observation(
        self,
    ) -> None:
        seen_steps: list[int] = []

        def model(req: dict[str, Any]) -> dict[str, Any]:
            seen_steps.append(req["step"])
            # Mutate request containers to prove trace and future steps stay detached.
            req["sources"]["doc"] = "TAMPERED"
            if req["step"] == 1:
                return {"kind": "tool", "name": "inspect", "arguments": {"target": "doc"}}
            if req["step"] == 2:
                return {"kind": "tool", "name": "odd_tool", "arguments": {}}
            self.assertEqual(req["history"][-1]["kind"], "tool_return")
            self.assertIn("non_json_value", req["history"][-1]["value"])
            return {"kind": "final", "answer": "verified-complete"}

        def inspect_tool(args: dict[str, Any]) -> dict[str, Any]:
            args["target"] = "MUTATED"
            return {"observed": "clean"}

        def odd_tool(_args: dict[str, Any]) -> object:
            return _ExplodingRepr()

        out = run_agent(
            task="Inspect source and conclude.",
            sources={"doc": "immutable-body"},
            model=model,
            tools={"inspect": inspect_tool, "odd_tool": odd_tool},
            max_steps=4,
        )
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["answer"], "verified-complete")
        self.assertEqual(out["model_calls"], 3)
        self.assertEqual(out["tool_calls"], 2)
        self.assertIsNone(out["error"])
        self.assertEqual(seen_steps, [1, 2, 3])
        self.assertEqual(out["trace"]["sources"]["doc"]["body"], "immutable-body")
        tool_call_ev = next(e for e in out["trace"]["events"] if e["kind"] == "tool_call")
        self.assertEqual(tool_call_ev["arguments"], {"target": "doc"})

    def test_error_and_budget_boundaries(self) -> None:
        # 1. Zero step budget terminates cleanly with step_limit.
        zero = run_agent(
            task="t",
            sources={},
            model=lambda _r: {"kind": "final", "answer": "x"},
            tools={},
            max_steps=0,
        )
        self.assertEqual(zero["status"], "step_limit")
        self.assertEqual(zero["model_calls"], 0)

        # 2. Exhausted step budget returns step_limit.
        exhaust = run_agent(
            task="t",
            sources={},
            model=lambda _r: {"kind": "tool", "name": "ping", "arguments": {}},
            tools={"ping": lambda _a: {"ok": True}},
            max_steps=2,
        )
        self.assertEqual(exhaust["status"], "step_limit")
        self.assertEqual(exhaust["model_calls"], 2)
        self.assertEqual(exhaust["tool_calls"], 2)

        # 3. Malformed model returns fail closed with explicit codes.
        for bad_action in (
            float("nan"),
            ["not-a-dict"],
            {"kind": "unknown"},
            {"kind": "final", "answer": 123},
            {"kind": "tool", "name": 99, "arguments": {}},
            {"kind": "tool", "name": "x", "arguments": "not-a-dict"},
        ):
            res = run_agent(
                task="t", sources={}, model=lambda _r, v=bad_action: v, tools={}, max_steps=1
            )
            self.assertEqual(res["status"], "malformed_model_return")
            self.assertEqual(res["error"]["code"], "malformed_model_return")

        # 4. Unknown tool fails closed.
        unk = run_agent(
            task="t",
            sources={},
            model=lambda _r: {"kind": "tool", "name": "missing", "arguments": {}},
            tools={},
            max_steps=1,
        )
        self.assertEqual(unk["status"], "unknown_tool")
        self.assertEqual(unk["error"]["name"], "missing")

        # 5. Model and tool exceptions are captured in callback_error events.
        def boom_model(_r: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("model failed")

        m_err = run_agent(task="t", sources={}, model=boom_model, tools={}, max_steps=2)
        self.assertEqual(m_err["status"], "model_error")
        self.assertEqual(m_err["error"]["exception_type"], "RuntimeError")
        self.assertEqual(m_err["model_calls"], 1)

        def boom_tool(_a: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("tool failed")

        t_err = run_agent(
            task="t",
            sources={},
            model=lambda _r: {"kind": "tool", "name": "boom", "arguments": {}},
            tools={"boom": boom_tool},
            max_steps=2,
        )
        self.assertEqual(t_err["status"], "tool_error")
        self.assertEqual(t_err["error"]["name"], "boom")
        self.assertEqual(t_err["error"]["exception_type"], "ValueError")
        self.assertEqual(t_err["tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
