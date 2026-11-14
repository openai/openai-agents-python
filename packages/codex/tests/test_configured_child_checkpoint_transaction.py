"""Host transaction tests have been injecting writer outcomes without a native producer."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from _agents_codex._produced_prefix import _ProducedPrefix
from _agents_codex.session import (
    Session,
    _CheckpointPhase,
    _ConfiguredChild,
    _SuspendBoundary,
)
from _agents_codex.usage import RunUsage


class Saved:
    def __init__(self, name):
        self.name = name

    def to_json(self):
        return json.dumps({"fixture": self.name})


class Child:
    def __init__(self, name, log, failure=None):
        self.name, self.log, self.failure = name, log, failure

    async def _suspend_ready(self, ready, *, _phase):
        self.log.append(("seal", self.name))
        if self.failure:
            _phase.state = self.failure
            raise ValueError("child refusal")
        _phase.state = "sealed"
        return Saved(self.name)


class Parent:
    _configured_checkpoint_children = Session._configured_checkpoint_children

    def __init__(self, child_failure=None, stage=None, parent_failure=None):
        self.log = []
        self._closing = self._closed = self._broken = False
        self._configured_capture = self._sealed_checkpoint = None
        self._profiles = {}
        self._storage = None
        self._reader = asyncio.get_running_loop().create_future()
        self._active = SimpleNamespace(
            callbacks=set(),
            restored_pending=False,
            stopping=False,
            failure=asyncio.get_running_loop().create_future(),
            run_usage=SimpleNamespace(snapshot=lambda: RunUsage(0, 0, 0, 0, 0, 0, 0, 0)),
        )
        self._active.add_callback = self._active.callbacks.add
        self._configured_children = []
        self.stage, self.parent_failure = stage, parent_failure
        self._native = SimpleNamespace(_respond_python_tool=self.native_reply)
        for index, name in enumerate(("left", "right")):
            child = _ConfiguredChild(
                self._active,
                Child(name, self.log, child_failure if index == 1 else None),
                lambda: not self._closing,
                callback_task=asyncio.get_running_loop().create_future(),
                parent_request=json.dumps({"callId": name, "_sdk_invocation": index + 1}),
                parent_callback={"fixture": name},
            )

            async def recover(child=child, name=name):
                self.log.append(("recover", name))
                child.checkpoint = None
                child.sealing = False
                child.recovery_ready.set_result(None)
                await asyncio.Future()

            child.recover = recover
            self._configured_children.append(child)
            self._active.callbacks.add(child.callback_task)

    def _check_portable_capture(self, **kwargs):
        pass

    def _check_manual_capture(self, active):
        assert active is self._active

    def _configured_checkpoint_usage(self, saved):
        return RunUsage(0, 0, 0, 0, 0, 0, 0, 0)

    async def native_reply(self, payload):
        payload = json.loads(payload)
        self.log.append(
            (
                "rollback" if payload.get("rollback") else "park",
                tuple(
                    entry["request"]["callId"] for entry in payload["_sdk_configured_continuations"]
                ),
            )
        )
        if payload.get("rollback"):
            return True
        if self.stage == "uncertain":
            raise RuntimeError("unknown native outcome")
        return self.stage != "refused"

    async def _checkpoint(self, active, *, before_seal=None, _phase):
        self.log.append(("parent", "seal"))
        if self.parent_failure:
            _phase.state = self.parent_failure
            raise ValueError("parent refusal")
        _phase.state = "sealed"
        return Saved("parent")

    async def aclose(self):
        assert self._configured_capture is None
        self.log.append(("parent", "close"))
        self._closed = self._closing = True
        for task in self._active.callbacks:
            task.cancel()
        await asyncio.gather(*self._active.callbacks, return_exceptions=True)

    async def run(self):
        ready = _SuspendBoundary(
            self, self._active, tuple((child, None) for child in self._configured_children)
        )
        return await Session._suspend_ready(self, ready)

    async def cleanup(self):
        for task in self._active.callbacks:
            task.cancel()
        await asyncio.gather(*self._active.callbacks, return_exceptions=True)


@pytest.mark.asyncio
async def test_second_child_known_refusal_recovers_first_without_native_park():
    parent = Parent(child_failure="refused")
    try:
        with pytest.raises(ValueError, match="child refusal"):
            await parent.run()
        assert parent.log == [("seal", "left"), ("seal", "right"), ("recover", "left")]
        assert not parent._closed
        assert (
            parent._configured_children[0].parent_request
            == '{"callId": "left", "_sdk_invocation": 1}'
        )
    finally:
        await parent.cleanup()


@pytest.mark.asyncio
async def test_known_parent_preseal_refusal_rolls_back_complete_tuple_before_recovery():
    parent = Parent(parent_failure="refused")
    try:
        with pytest.raises(ValueError, match="parent refusal"):
            await parent.run()
        assert parent.log == [
            ("seal", "left"),
            ("seal", "right"),
            ("park", ("left", "right")),
            ("parent", "seal"),
            ("rollback", ("left", "right")),
            ("recover", "left"),
            ("recover", "right"),
        ]
        assert not parent._closed
    finally:
        await parent.cleanup()


@pytest.mark.asyncio
async def test_authoritative_native_batch_refusal_recovers_without_rollback():
    parent = Parent(stage="refused")
    try:
        with pytest.raises(RuntimeError, match="continuation expired"):
            await parent.run()
        assert ("park", ("left", "right")) in parent.log
        assert not any(entry[0] == "rollback" for entry in parent.log)
        assert [entry for entry in parent.log if entry[0] == "recover"] == [
            ("recover", "left"),
            ("recover", "right"),
        ]
        assert not parent._closed
    finally:
        await parent.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["child", "stage", "parent-uncertain", "parent-sealed"])
async def test_uncertain_or_sealed_outcome_retires_without_live_rollback(fault):
    parent = Parent(
        child_failure="uncertain" if fault == "child" else None,
        stage="uncertain" if fault == "stage" else None,
        parent_failure=fault.removeprefix("parent-") if fault.startswith("parent-") else None,
    )
    try:
        with pytest.raises((ValueError, RuntimeError)):
            await parent.run()
        assert parent._closed
        assert not any(entry[0] in {"rollback", "recover"} for entry in parent.log)
        assert all(child.seal_hold.is_set() for child in parent._configured_children)
    finally:
        await parent.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("outcome", ["refused", "uncertain", "sealed"])
async def test_checkpoint_phase_classifies_only_authoritative_native_refusal(restored, outcome):
    async def seal(**kwargs):
        if outcome == "uncertain":
            raise RuntimeError("unknown native failure")
        return json.dumps(
            None if outcome == "refused" else {"checkpoint": {}, "model_attempt": None}
        )

    async def ready(active):
        return True

    active = SimpleNamespace(
        budget=None,
        operation_refs=frozenset(),
        operation_mcp=(),
        output=SimpleNamespace(mode="text"),
        output_override=False,
        tracing=None,
        restored_pending=restored,
        stopping=False,
        terminal=asyncio.Future(),
        failure=asyncio.Future(),
    )
    parent = SimpleNamespace(
        _executor=None,
        _native_agents=None,
        _project_guidance=None,
        _strict_executor_writes=False,
        _profiles={},
        _reader=asyncio.Future(),
        _native=SimpleNamespace(_seal_manual=seal, _export_mcp_approval_choices=lambda: "{}"),
        _capture_produced_checkpoint_prefix=False,
        _produced_prefix=_ProducedPrefix(),
        _manual_ready=ready,
        _checkpoint_guardrail_records=lambda _: [],
        _broken=False,
        _observations=SimpleNamespace(notify=lambda: None),
        _current_remembered_approvals=lambda: (_ for _ in ()).throw(RuntimeError("postseal")),
    )
    phase = _CheckpointPhase()
    with pytest.raises(RuntimeError):
        await Session._capture_checkpoint(parent, active, None, False, _phase=phase)
    assert phase.state == outcome
    assert phase.refused_before_seal is (outcome == "refused")


def test_parallel_child_usage_merges_each_saved_prefix_once(monkeypatch):
    from dataclasses import asdict

    from _agents_codex import session as runtime

    def usage(requests):
        return RunUsage(11 * requests, 0, 0, 3 * requests, 0, 14 * requests, requests, 0)

    children = {
        name: SimpleNamespace(run_usage=usage(1), native_continuation=None)
        for name in ("left", "right")
    }
    monkeypatch.setattr(
        runtime.SessionState,
        "from_json",
        staticmethod(lambda text: children[json.loads(text)["child"]]),
    )
    saved = SimpleNamespace(
        run_usage=usage(3),
        native_continuation=SimpleNamespace(
            pending_manual=[
                SimpleNamespace(
                    continuation={
                        "kind": "configured-child-v1",
                        "state": {"child": name},
                        "usage_in_parent": asdict(usage(1)),
                    }
                )
                for name in children
            ]
        ),
    )
    assert Session._configured_checkpoint_usage(saved) == usage(3)
    children["right"].run_usage = usage(2)
    assert Session._configured_checkpoint_usage(saved) == usage(4)
