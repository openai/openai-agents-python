"""Check fresh-run ownership without loading a native SDK or measuring performance."""

from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import validate_fresh_tool_rows
from workloads import fresh_native_supported, get_workload

IN_CALLBACK = contextvars.ContextVar("benchmark_test_callback", default=False)


class Context:
    def __class_getitem__(cls, item):
        return cls


class Turn:
    def __init__(self, operation, *, gap=False, missing_terminal=False):
        self.session_id, self.submission_id = f"session-{operation}", operation
        self.released = asyncio.Event()
        self.gap = gap
        self.missing_terminal = missing_terminal
        self.closed_cursors = []
        self.late_drained = False

    def events(self, after=0):
        async def iterate():
            def event(sequence, kind, **values):
                assert not IN_CALLBACK.get()
                return SimpleNamespace(
                    sequence=sequence,
                    type=kind,
                    session_id=self.session_id,
                    submission_id=self.submission_id,
                    **values,
                )

            try:
                if after == 0:
                    yield event(1, "gap" if self.gap else "output_accepted")
                    await self.released.wait()
                    yield event(2, "output_text_delta", delta="first")
                    yield event(3, "output_text_delta", delta="last")
                    if not self.missing_terminal:
                        yield event(4, "turn_finished", status="completed")
                else:
                    assert after == 3
                    yield event(4, "turn_finished", status="completed")
                    self.late_drained = True
            finally:
                self.closed_cursors.append(after)

        return iterate()


class Agent:
    def __init__(self, *, tools, **options):
        assert "provider" not in options
        self.provider, self.callback = None, tools[0]
        self.gap = self.mismatch = self.hold = self.missing_terminal = False
        self.started = asyncio.Event()

    async def run(self, prompt):
        operation = prompt.split("=", 1)[1].split(" ", 1)[0]
        self.started.set()
        if self.hold:
            await asyncio.Event().wait()
        self.turn = Turn(operation, gap=self.gap, missing_terminal=self.missing_terminal)
        context = Context()
        context.context, context.submission = None, self.turn
        context.call_id = f"call_{operation}_0_0"
        token = IN_CALLBACK.set(True)
        try:
            self.output = await self.callback(context, f"echo:{operation}:0:0")
        finally:
            IN_CALLBACK.reset(token)
        self.turn.released.set()
        return SimpleNamespace(
            final_output="firstlast", turn=Turn("wrong") if self.mismatch else self.turn
        )


def adapter(stream):
    module = SimpleNamespace(
        Agent=Agent,
        ToolContext=Context,
        function_tool=lambda callback: callback,
        NativeProvider=mock.Mock(side_effect=AssertionError("unexpected owner")),
        telemetry=SimpleNamespace(configure=mock.AsyncMock(), shutdown=mock.AsyncMock()),
    )
    path = Path(__file__).resolve().parents[1] / "v1_adapter.py"
    spec = importlib.util.spec_from_file_location("fresh_adapter_test", path)
    loaded = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"agents": module}):
        spec.loader.exec_module(loaded)
        return loaded.Adapter(
            "unused",
            default_host=True,
            provider_mode="omitted",
            with_tool=True,
            stream=stream,
            transport="websocket",
        )


class FreshToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_reader_owns_first_read_and_skips_unneeded_suffix(self):
        async with adapter(True) as value:
            result = await value.run("BENCH_RUN=first input")
            self.assertEqual(result["deltas"], ["first", "last"])
            self.assertTrue(result["reader_ready"] and result["stream_fully_drained"])
            self.assertTrue(
                result["tool_context_validated"] and result["context_submission_is_result_turn"]
            )
            self.assertEqual(value.agent.turn.closed_cursors, [0])
            self.assertFalse(value.agent.turn.late_drained)
            self.assertEqual(value._pending, {})
            other = await value.run("BENCH_RUN=second input")
            self.assertNotEqual(result["native_session_id"], other["native_session_id"])
            self.assertEqual(value.tool_calls, 2)

    async def test_missing_terminal_is_drained_from_the_unread_suffix(self):
        async with adapter(True) as value:
            value.agent.missing_terminal = True
            result = await value.run("BENCH_RUN=suffix input")
            self.assertEqual(result["deltas"], ["first", "last"])
            self.assertTrue(result["stream_fully_drained"])
            self.assertTrue(value.agent.turn.late_drained)
            self.assertEqual(value.agent.turn.closed_cursors, [0, 3])
            self.assertEqual(value._pending, {})

    async def test_returned_turn_mismatch_is_rejected_without_retaining_context(self):
        async with adapter(False) as value:
            value.agent.mismatch = True
            with self.assertRaises(AssertionError):
                await value.run("BENCH_RUN=bad input")
            self.assertEqual(value._pending, {})

    async def test_reader_failure_releases_waiting_callback_and_context(self):
        async with adapter(True) as value:
            value.agent.gap = True
            with self.assertRaises(AssertionError):
                await asyncio.wait_for(value.run("BENCH_RUN=gap input"), 2)
            self.assertEqual(value._pending, {})
            self.assertEqual(value.agent.turn.closed_cursors, [0])

    async def test_cancelled_run_joins_its_waiting_reader(self):
        async with adapter(True) as value:
            value.agent.hold = True
            previous = asyncio.all_tasks()
            task = asyncio.create_task(value.run("BENCH_RUN=cancel input"))
            await value.agent.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(value._pending, {})
            self.assertEqual(asyncio.all_tasks(), previous)


class FreshCaseTests(unittest.TestCase):
    def test_dense_variants_keep_the_same_identity_family(self):
        names = [f"oneshot_tool_output_16k_{n}" for n in (1, 16, 128)] + [
            "oneshot_tool_output_16k_128_active"
        ]
        for name in names:
            case = get_workload(name)
            self.assertTrue(fresh_native_supported(case))
            self.assertEqual(case.identity_family, "oneshot_tool_output_16k")
            self.assertEqual(
                (case.output_chars, case.tools_per_round, case.tool_rounds), (16384, 1, 1)
            )
        self.assertTrue(fresh_native_supported(get_workload("oneshot_tool")))
        self.assertFalse(fresh_native_supported(get_workload("streamed_tool")))

    def test_identity_validator_rejects_reuse_and_unjoined_reader(self):
        case = get_workload("oneshot_tool", iterations=1)
        rows = [
            {
                "native_session_id": f"session-{n}",
                "tool_context_validated": True,
                "context_submission_is_result_turn": True,
            }
            for n in range(2)
        ]
        validate_fresh_tool_rows(case, rows)
        for key, value in (
            ("native_session_id", "session-0"),
            ("tool_context_validated", False),
            ("context_submission_is_result_turn", False),
        ):
            changed = [rows[0], {**rows[1], key: value}]
            with self.assertRaises(ValueError):
                validate_fresh_tool_rows(case, changed)
        with self.assertRaises(ValueError):
            validate_fresh_tool_rows(
                get_workload("oneshot_tool_output_16k_128_active", iterations=1), rows
            )
