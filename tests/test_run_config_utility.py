"""Tests for RunConfig utility methods."""

from __future__ import annotations

from agents.run import RunConfig


class TestRunConfigUtilityMethods:
    """Tests for RunConfig utility methods."""

    def test_repr_default_config(self) -> None:
        """Default RunConfig should have concise repr."""
        config = RunConfig()
        assert repr(config) == "RunConfig()"

    def test_repr_with_model(self) -> None:
        """RunConfig with model should show model in repr."""
        config = RunConfig(model="gpt-4o")
        assert "model='gpt-4o'" in repr(config)

    def test_repr_with_custom_workflow_name(self) -> None:
        """RunConfig with custom workflow name should show it."""
        config = RunConfig(workflow_name="Custom Flow")
        assert "workflow_name='Custom Flow'" in repr(config)

    def test_repr_with_tracing_disabled(self) -> None:
        """RunConfig with tracing disabled should show it."""
        config = RunConfig(tracing_disabled=True)
        assert "tracing_disabled=True" in repr(config)

    def test_repr_with_trace_id(self) -> None:
        """RunConfig with trace_id should show it."""
        config = RunConfig(trace_id="trace-123")
        assert "trace_id=" in repr(config)

    def test_copy_creates_new_instance(self) -> None:
        """copy() should create a new instance."""
        original = RunConfig(workflow_name="Original")
        copied = original.copy()
        assert copied is not original
        assert copied.workflow_name == original.workflow_name

    def test_copy_with_overrides(self) -> None:
        """copy() should apply overrides."""
        original = RunConfig(workflow_name="Original", tracing_disabled=False)
        copied = original.copy(workflow_name="Modified", tracing_disabled=True)
        
        assert original.workflow_name == "Original"
        assert original.tracing_disabled is False
        
        assert copied.workflow_name == "Modified"
        assert copied.tracing_disabled is True

    def test_copy_preserves_other_fields(self) -> None:
        """copy() should preserve fields not in overrides."""
        original = RunConfig(
            workflow_name="Test",
            trace_id="trace-123",
            group_id="group-456",
        )
        copied = original.copy(workflow_name="Modified")
        
        assert copied.trace_id == "trace-123"
        assert copied.group_id == "group-456"
        assert copied.workflow_name == "Modified"
