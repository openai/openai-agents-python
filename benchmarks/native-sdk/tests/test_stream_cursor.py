"""Exercise the adapter's cursor handling without importing an SDK."""

from __future__ import annotations

import importlib.util
import unittest
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


def conversation_class():
    agents = ModuleType("agents")
    for name in ("Agent", "NativeProvider", "function_tool", "telemetry"):
        setattr(agents, name, None)
    path = Path(__file__).resolve().parents[1] / "v1_adapter.py"
    spec = importlib.util.spec_from_file_location("cursor_test_adapter", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"agents": agents}):
        spec.loader.exec_module(module)
    return module.Conversation


class RetainedSession:
    """Model the SDK's global bounded suffix and turn-filtered cursor."""

    def __init__(self):
        self.records = deque(maxlen=256)
        self.sequence = 0
        self.gaps = 0

    async def submit(self, prompt):
        for kind in ("output_text_delta", "turn_finished"):
            self.sequence += 1
            self.records.append(SimpleNamespace(type=kind, sequence=self.sequence, delta=prompt))
        return RetainedTurn(self, prompt, self.sequence - 1)


class RetainedTurn:
    def __init__(self, session, text, first):
        self.session = session
        self.text = text
        self.first = first

    async def events(self, *, after=0):
        oldest = self.session.records[0].sequence
        if after < oldest - 1:
            self.session.gaps += 1
            yield SimpleNamespace(type="gap", sequence=oldest - 1)
        for event in self.session.records:
            if event.sequence > after and event.sequence >= self.first:
                yield event

    async def get_result(self):
        return SimpleNamespace(final_output=self.text)


class StreamCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_turns_continue_after_the_last_observed_event(self):
        session = RetainedSession()
        conversation = conversation_class()(session, True)
        for index in range(160):
            prompt = str(index)
            result = await conversation.run(prompt)
            self.assertEqual(result["text"], prompt)
            self.assertEqual(result["deltas"], [prompt])
            self.assertIsNotNone(result["first_delta_ns"])
        self.assertGreater(session.records[0].sequence, 1)
        self.assertEqual(session.gaps, 0)
        self.assertEqual(conversation.after, session.sequence)

    async def test_a_real_gap_type_is_rejected(self):
        session = RetainedSession()
        for index in range(130):
            await session.submit(str(index))
        conversation = conversation_class()(session, True)
        with self.assertRaisesRegex(AssertionError, "dropped observations"):
            await conversation.run("after retention")
        self.assertEqual(session.gaps, 1)


if __name__ == "__main__":
    unittest.main()
