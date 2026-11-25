"""Pydantic AI provider tests with a scripted local model: no network, no API cost."""
from __future__ import annotations

import copy
import unittest

try:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
except ImportError:  # optional dependency: pip install 'rf-sentinel[analyst-llm]'
    FunctionModel = None

from rf_platform.analyst.deterministic import DeterministicAnalyst
from rf_platform.analyst.investigate import run_investigation
from rf_platform.analyst.tools import ToolGateway
from tests.test_analyst import BEACON, load_payloads, store_from


def scripted(store, final_outputs, opening=None):
    """Model that calls read-only tools, then submits each queued draft in turn."""
    calls = []
    scope = {"reference_snapshot_id": store.order[0], "current_snapshot_id": store.order[-1]}
    good = DeterministicAnalyst().investigate(ToolGateway(store), scope)  # same tool args → same computed IDs

    def model(messages, info):
        calls.append(len(messages))
        step = len(calls)
        if step == 1:
            parts = [ToolCallPart("compare_baselines", {"reference_snapshot_id": scope["reference_snapshot_id"],
                                                         "current_snapshot_id": scope["current_snapshot_id"]}),
                     ToolCallPart("get_environment_summary", {"snapshot_id": scope["reference_snapshot_id"]}),
                     ToolCallPart("get_environment_summary", {"snapshot_id": scope["current_snapshot_id"]})]
            return ModelResponse(parts=[*(opening or []), *parts])
        if step == 2:
            return ModelResponse(parts=[ToolCallPart("get_entity_history", {"entity_key": BEACON})])
        draft = final_outputs[min(step - 3, len(final_outputs) - 1)](copy.deepcopy(good))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, draft)])
    return FunctionModel(model, model_name="scripted-test"), calls


def fabricate(draft):
    draft["findings"][0]["observed_facts"][0]["ref"] = "event:invented-0001"
    return draft


def keep(draft):
    return draft


@unittest.skipIf(FunctionModel is None, "pydantic-ai not installed")
class PydanticAIProviderTest(unittest.TestCase):
    def provider(self, model, **kwargs):
        from rf_platform.analyst.llm import PydanticAIAnalyst
        return PydanticAIAnalyst(model, **kwargs)

    def test_tool_calls_go_through_gateway_and_output_is_grounded(self):
        store = store_from(load_payloads())
        model, _ = scripted(store, [keep])
        inv = run_investigation(store, self.provider(model))
        self.assertEqual(inv["provider"]["name"], "pydantic-ai")
        self.assertNotIn("fallback", inv["provider"])
        self.assertTrue(inv["grounding"]["passed"], inv["grounding"]["issues"])
        self.assertEqual([a["tool"] for a in inv["tool_audit"]],
                         ["compare_baselines", "get_environment_summary", "get_environment_summary", "get_entity_history"])
        self.assertEqual(inv["executed_actions"], [])

    def test_fabricated_reference_is_sent_back_and_corrected(self):
        store = store_from(load_payloads())
        model, calls = scripted(store, [fabricate, keep])
        inv = run_investigation(store, self.provider(model))
        self.assertEqual(len(calls), 4)  # two tool turns, rejected draft, corrected draft
        self.assertTrue(inv["grounding"]["passed"])
        self.assertNotIn("invented-0001", str(inv["findings"]))

    def test_unoffered_restricted_tool_never_reaches_execution(self):
        store = store_from(load_payloads())
        model, _ = scripted(store, [keep], opening=[ToolCallPart("start_passive_capture", {"sensor_id": "demo-bladerf:0"})])
        inv = run_investigation(store, self.provider(model))
        self.assertNotIn("start_passive_capture", [a["tool"] for a in inv["tool_audit"]])
        self.assertTrue(inv["grounding"]["passed"])
        self.assertEqual(inv["executed_actions"], [])

    def test_persistent_fabrication_falls_back_to_deterministic(self):
        store = store_from(load_payloads())
        model, _ = scripted(store, [fabricate])
        inv = run_investigation(store, self.provider(model, output_retries=1))
        self.assertEqual(inv["provider"]["fallback"], "deterministic")
        self.assertTrue(inv["provider"]["failed"])
        self.assertTrue(inv["grounding"]["passed"])

    def test_claude_defaults_enable_refusal_fallback(self):
        settings = self.provider("anthropic:claude-opus-5-5")._settings()
        self.assertEqual(settings["extra_body"], {"fallbacks": "default"})
        self.assertIn("server-side-fallback-2026-07-01", settings["anthropic_betas"])
        self.assertIsNone(self.provider("ollama:llama3.3")._settings())


if __name__ == "__main__":
    unittest.main()
