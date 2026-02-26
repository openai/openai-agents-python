from __future__ import annotations

from uuid import uuid4

from agents.tracing import TracingConfig, set_tracing_disabled, trace
from agents.tracing.context import create_trace_for_run
from agents.tracing.scope import Scope
from agents.tracing.traces import NoOpTrace, ReattachedTrace, TraceImpl, TraceState


def _new_trace_id() -> str:
    return f"trace_{uuid4().hex}"


def _mark_trace_as_started(
    *,
    workflow_name: str = "workflow",
    group_id: str | None = "group-1",
    metadata: dict[str, str] | None = None,
    tracing_api_key: str | None = None,
) -> TraceState:
    metadata = metadata or {"key": "value"}
    trace_id = _new_trace_id()
    Scope.set_current_trace(None)
    set_tracing_disabled(False)

    original = trace(
        workflow_name=workflow_name,
        trace_id=trace_id,
        group_id=group_id,
        metadata=metadata,
        tracing={"api_key": tracing_api_key} if tracing_api_key is not None else None,
    )
    assert isinstance(original, TraceImpl)
    original.start()
    original.finish()

    trace_state = TraceState.from_trace(original)
    assert trace_state is not None
    return trace_state


def test_create_trace_for_run_reattaches_matching_started_trace() -> None:
    trace_state = _mark_trace_as_started(tracing_api_key="trace-key")

    created = create_trace_for_run(
        workflow_name="workflow",
        trace_id=trace_state.trace_id,
        group_id=trace_state.group_id,
        metadata=dict(trace_state.metadata or {}),
        tracing={"api_key": "trace-key"},
        disabled=False,
        trace_state=trace_state,
        reattach_resumed_trace=True,
    )

    assert isinstance(created, ReattachedTrace)
    assert created.trace_id == trace_state.trace_id


def test_create_trace_for_run_does_not_reattach_unseen_trace_id() -> None:
    Scope.set_current_trace(None)
    set_tracing_disabled(False)
    trace_state = TraceState(
        trace_id=_new_trace_id(),
        workflow_name="workflow",
        group_id="group-1",
        metadata={"key": "value"},
    )

    created = create_trace_for_run(
        workflow_name="workflow",
        trace_id=trace_state.trace_id,
        group_id=trace_state.group_id,
        metadata=dict(trace_state.metadata or {}),
        tracing=None,
        disabled=False,
        trace_state=trace_state,
        reattach_resumed_trace=True,
    )

    assert isinstance(created, TraceImpl)
    assert not isinstance(created, ReattachedTrace)


def test_create_trace_for_run_does_not_reattach_when_settings_mismatch() -> None:
    trace_state = _mark_trace_as_started(tracing_api_key="trace-key")

    mismatch_cases: list[tuple[str, str | None, dict[str, str], TracingConfig]] = [
        (
            "workflow-override",
            trace_state.group_id,
            dict(trace_state.metadata or {}),
            {"api_key": "trace-key"},
        ),
        (
            "workflow",
            "group-override",
            dict(trace_state.metadata or {}),
            {"api_key": "trace-key"},
        ),
        (
            "workflow",
            trace_state.group_id,
            {"key": "override"},
            {"api_key": "trace-key"},
        ),
        (
            "workflow",
            trace_state.group_id,
            dict(trace_state.metadata or {}),
            {"api_key": "other-trace-key"},
        ),
    ]

    for workflow_name, group_id, metadata, tracing in mismatch_cases:
        Scope.set_current_trace(None)
        created = create_trace_for_run(
            workflow_name=workflow_name,
            trace_id=trace_state.trace_id,
            group_id=group_id,
            metadata=metadata,
            tracing=tracing,
            disabled=False,
            trace_state=trace_state,
            reattach_resumed_trace=True,
        )

        assert isinstance(created, TraceImpl)
        assert not isinstance(created, ReattachedTrace)


def test_create_trace_for_run_respects_disabled_flag_for_resume() -> None:
    trace_state = _mark_trace_as_started()

    created = create_trace_for_run(
        workflow_name="workflow",
        trace_id=trace_state.trace_id,
        group_id=trace_state.group_id,
        metadata=dict(trace_state.metadata or {}),
        tracing=None,
        disabled=True,
        trace_state=trace_state,
        reattach_resumed_trace=True,
    )

    assert isinstance(created, NoOpTrace)


def test_create_trace_for_run_uses_existing_current_trace() -> None:
    trace_state = _mark_trace_as_started()
    outer_trace = trace(workflow_name="outer", trace_id=_new_trace_id())
    assert isinstance(outer_trace, TraceImpl)

    with outer_trace:
        created = create_trace_for_run(
            workflow_name="workflow",
            trace_id=trace_state.trace_id,
            group_id=trace_state.group_id,
            metadata=dict(trace_state.metadata or {}),
            tracing=None,
            disabled=False,
            trace_state=trace_state,
            reattach_resumed_trace=True,
        )

        assert created is None
