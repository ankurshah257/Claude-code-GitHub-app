"""Tests for holding generation. No network and no API calls.

The Claude client is faked, which exercises everything this module actually
owns: request shape, response parsing, refusal handling, provenance, cost
arithmetic, and the "this order decides nothing" path.
"""

from __future__ import annotations

import unittest

from judgments.holding import Provenance
from judgments.summarize import (
    HOLDING_SCHEMA,
    TOOL_NAME,
    Summariser,
    Usage,
)


class FakeBlock:
    def __init__(self, name, data):
        self.type = "tool_use"
        self.name = name
        self.input = data


class FakeUsage:
    def __init__(self, i=1000, o=100, c=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = c


class FakeMessage:
    def __init__(self, content, stop_reason="tool_use", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class FakeClient:
    """Records the request it was given and returns a scripted reply."""

    def __init__(self, reply):
        self._reply = reply
        self.request = None
        self.messages = self

    def create(self, **kwargs):
        self.request = kwargs
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def holding_reply(states, text, usage=None):
    return FakeMessage(
        [FakeBlock(TOOL_NAME, {"states_a_holding": states, "holding": text})],
        usage=usage,
    )


class TestRequestShape(unittest.TestCase):
    """The request must be built the way the API expects."""

    def _request(self, text="Some judgment text here."):
        client = FakeClient(holding_reply(True, "The court held X."))
        Summariser(client=client).summarise(text, title="A v. B")
        return client.request

    def test_uses_opus_5_by_default(self):
        self.assertEqual(self._request()["model"], "claude-opus-5")

    def test_adaptive_thinking(self):
        # budget_tokens is rejected on this model; adaptive is the only on-mode.
        self.assertEqual(self._request()["thinking"], {"type": "adaptive"})
        self.assertNotIn("budget_tokens", str(self._request()["thinking"]))

    def test_effort_is_inside_output_config(self):
        self.assertEqual(self._request()["output_config"], {"effort": "high"})

    def test_output_is_structurally_forced(self):
        req = self._request()
        tool = req["tools"][0]
        self.assertTrue(tool["strict"])
        self.assertEqual(req["tool_choice"], {"type": "tool", "name": TOOL_NAME})

    def test_strict_schema_is_closed(self):
        # strict tool use requires both of these or the API rejects it.
        self.assertFalse(HOLDING_SCHEMA["additionalProperties"])
        self.assertIn("states_a_holding", HOLDING_SCHEMA["required"])

    def test_system_prompt_is_cached(self):
        # The system prompt is identical for every judgment in a run, so it is
        # the one part worth caching across tens of thousands of calls.
        self.assertEqual(
            self._request()["system"][0]["cache_control"], {"type": "ephemeral"}
        )

    def test_judgment_is_passed_in_the_user_turn(self):
        req = self._request("DISTINCTIVE JUDGMENT BODY")
        self.assertIn("DISTINCTIVE JUDGMENT BODY", req["messages"][0]["content"])


class TestSummarise(unittest.TestCase):
    def test_holding_is_marked_generated_not_headnote(self):
        # The whole point of the provenance field: this must never be mistaken
        # for the court's own words.
        client = FakeClient(holding_reply(True, "Limitation runs from the decree."))
        result = Summariser(client=client).summarise("text")
        self.assertIs(result.holding.provenance, Provenance.GENERATED)
        self.assertFalse(result.holding.is_verbatim)
        self.assertEqual(result.holding.text, "Limitation runs from the decree.")

    def test_procedural_order_is_recorded_as_deciding_nothing(self):
        client = FakeClient(holding_reply(False, "Adjourned to 3 March."))
        result = Summariser(client=client).summarise("text")
        self.assertFalse(result.states_a_holding)
        # Still stored: knowing an order decided nothing is a real answer.
        self.assertEqual(result.holding.text, "Adjourned to 3 March.")

    def test_refusal_yields_no_holding(self):
        # A safety decline must not be written into the database as if the
        # court had said it.
        client = FakeClient(
            FakeMessage([FakeBlock(TOOL_NAME, {"states_a_holding": True,
                                               "holding": "..."})],
                        stop_reason="refusal")
        )
        result = Summariser(client=client).summarise("text")
        self.assertIs(result.holding.provenance, Provenance.NONE)
        self.assertEqual(result.holding.text, "")

    def test_missing_tool_call_yields_no_holding(self):
        client = FakeClient(FakeMessage([], stop_reason="end_turn"))
        result = Summariser(client=client).summarise("text")
        self.assertIs(result.holding.provenance, Provenance.NONE)

    def test_empty_holding_text_yields_no_holding(self):
        client = FakeClient(holding_reply(True, "   "))
        self.assertIs(
            Summariser(client=client).summarise("text").holding.provenance,
            Provenance.NONE,
        )

    def test_empty_input_makes_no_api_call(self):
        client = FakeClient(holding_reply(True, "x"))
        result = Summariser(client=client).summarise("")
        self.assertIsNone(client.request)
        self.assertIs(result.holding.provenance, Provenance.NONE)

    def test_long_judgment_is_truncated_and_disclosed(self):
        client = FakeClient(holding_reply(True, "held"))
        # The tail is kept over the middle: a holding sits at the end.
        text = "START" + ("x" * 200_000) + "CONCLUSION AND ORDER"
        Summariser(client=client, max_chars=1000).summarise(text)
        sent = client.request["messages"][0]["content"]
        self.assertIn("middle omitted", sent)
        self.assertIn("CONCLUSION AND ORDER", sent)


class TestUsage(unittest.TestCase):
    def test_cost_arithmetic(self):
        # 1M input at $5 plus 1M output at $25.
        self.assertAlmostEqual(Usage(1_000_000, 1_000_000).cost(), 30.0, places=6)

    def test_cache_reads_are_cheaper_than_fresh_input(self):
        fresh = Usage(input_tokens=1_000_000).cost()
        cached = Usage(cache_read_tokens=1_000_000).cost()
        self.assertLess(cached, fresh)
        self.assertAlmostEqual(cached, fresh * 0.1, places=6)

    def test_usage_accumulates(self):
        total = Usage(10, 1, 2) + Usage(20, 3, 4)
        self.assertEqual((total.input_tokens, total.output_tokens,
                          total.cache_read_tokens), (30, 4, 6))

    def test_usage_is_read_from_the_response(self):
        client = FakeClient(holding_reply(True, "x", usage=FakeUsage(4321, 99, 7)))
        result = Summariser(client=client).summarise("text")
        self.assertEqual(result.usage.input_tokens, 4321)
        self.assertEqual(result.usage.output_tokens, 99)
        self.assertEqual(result.usage.cache_read_tokens, 7)


if __name__ == "__main__":
    unittest.main()
