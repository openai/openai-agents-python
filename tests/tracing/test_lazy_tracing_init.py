import json
import subprocess
import sys
from typing import cast


def _run_python_snippet(snippet: str) -> dict[str, bool]:
    completed = subprocess.run(
        [sys.executable, "-c", snippet],
        check=True,
        capture_output=True,
        text=True,
    )
    return cast(dict[str, bool], json.loads(completed.stdout))


def test_import_does_not_initialize_default_tracing_objects():
    result = _run_python_snippet(
        """
import json
import agents.tracing as tracing
import agents.tracing.processors as processors
from agents.tracing import setup

print(json.dumps({
    "provider_is_none": setup.GLOBAL_TRACE_PROVIDER is None,
    "exporter_is_none": processors._global_exporter is None,
    "processor_is_none": processors._global_processor is None,
}))
"""
    )

    assert result["provider_is_none"] is True
    assert result["exporter_is_none"] is True
    assert result["processor_is_none"] is True


def test_get_trace_provider_initializes_defaults_lazily():
    result = _run_python_snippet(
        """
import json
import agents.tracing as tracing
import agents.tracing.processors as processors
from agents.tracing import setup

provider_1 = tracing.get_trace_provider()
provider_2 = tracing.get_trace_provider()

print(json.dumps({
    "same_provider": provider_1 is provider_2,
    "provider_is_set": setup.GLOBAL_TRACE_PROVIDER is not None,
    "exporter_created": processors._global_exporter is not None,
    "processor_created": processors._global_processor is not None,
    "processor_uses_exporter": (
        processors._global_processor is not None
        and processors._global_exporter is not None
        and processors._global_processor._exporter is processors._global_exporter
    ),
}))
"""
    )

    assert result["same_provider"] is True
    assert result["provider_is_set"] is True
    assert result["exporter_created"] is True
    assert result["processor_created"] is True
    assert result["processor_uses_exporter"] is True


def test_default_tracing_init_is_idempotent():
    result = _run_python_snippet(
        """
import json
import agents.tracing as tracing
import agents.tracing.processors as processors
from agents.tracing import setup

tracing._ensure_default_tracing_initialized()
provider_1 = setup.GLOBAL_TRACE_PROVIDER
tracing._ensure_default_tracing_initialized()
provider_2 = setup.GLOBAL_TRACE_PROVIDER

print(json.dumps({
    "same_provider": provider_1 is provider_2,
    "exporter_created": processors._global_exporter is not None,
    "processor_created": processors._global_processor is not None,
}))
"""
    )

    assert result["same_provider"] is True
    assert result["exporter_created"] is True
    assert result["processor_created"] is True


def test_set_trace_provider_sets_global_provider() -> None:
    from agents.tracing import setup
    from agents.tracing.provider import DefaultTraceProvider

    previous_provider = setup.GLOBAL_TRACE_PROVIDER
    provider = DefaultTraceProvider()
    try:
        setup.set_trace_provider(provider)
        assert setup.GLOBAL_TRACE_PROVIDER is provider
    finally:
        setup.GLOBAL_TRACE_PROVIDER = previous_provider


def test_trace_helper_initializes_defaults_lazily() -> None:
    result = _run_python_snippet(
        """
import json
import agents.tracing as tracing
import agents.tracing.processors as processors
from agents.tracing import setup

trace_obj = tracing.trace("lazy-init-from-trace")

print(json.dumps({
    "trace_created": trace_obj is not None,
    "provider_is_set": setup.GLOBAL_TRACE_PROVIDER is not None,
    "exporter_created": processors._global_exporter is not None,
    "processor_created": processors._global_processor is not None,
}))
"""
    )

    assert result["trace_created"] is True
    assert result["provider_is_set"] is True
    assert result["exporter_created"] is True
    assert result["processor_created"] is True
