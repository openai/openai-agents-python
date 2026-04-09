from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _run_python(script: str) -> dict[str, object]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    if pythonpath:
        env["PYTHONPATH"] = f"{SRC_ROOT}:{pythonpath}"
    else:
        env["PYTHONPATH"] = str(SRC_ROOT)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("Subprocess payload must be a JSON object.")
    return cast(dict[str, object], payload)


def test_import_agents_has_no_tracing_side_effects() -> None:
    payload = _run_python(
        """
import gc
import json
import httpx

clients_before = sum(1 for obj in gc.get_objects() if isinstance(obj, httpx.Client))
import agents  # noqa: F401
from agents.tracing import processors as tracing_processors
from agents.tracing import setup as tracing_setup
clients_after = sum(1 for obj in gc.get_objects() if isinstance(obj, httpx.Client))

print(
    json.dumps(
        {
            "client_delta": clients_after - clients_before,
            "provider_initialized": tracing_setup.GLOBAL_TRACE_PROVIDER is not None,
            "exporter_initialized": tracing_processors._global_exporter is not None,
            "processor_initialized": tracing_processors._global_processor is not None,
            "shutdown_handler_registered": tracing_setup._SHUTDOWN_HANDLER_REGISTERED,
        }
    )
)
"""
    )

    assert payload["client_delta"] == 0
    assert payload["provider_initialized"] is False
    assert payload["exporter_initialized"] is False
    assert payload["processor_initialized"] is False
    assert payload["shutdown_handler_registered"] is False


def test_import_agents_does_not_require_sqlite3() -> None:
    payload = _run_python(
        """
import importlib.abc
import json
import sys

class BlockSqlite3(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"sqlite3", "_sqlite3"}:
            raise ModuleNotFoundError(f"blocked optional backend module: {fullname}")
        return None

sys.meta_path.insert(0, BlockSqlite3())

import agents
from agents import Agent, Runner
from agents.memory import Session, SessionSettings

print(
    json.dumps(
        {
            "agent_name": Agent.__name__,
            "runner_name": Runner.__name__,
            "session_name": Session.__name__,
            "settings_name": SessionSettings.__name__,
            "sqlite3_loaded": "sqlite3" in sys.modules,
            "private_sqlite3_loaded": "_sqlite3" in sys.modules,
            "sqlite_session_loaded": "agents.memory.sqlite_session" in sys.modules,
            "sqlite_session_exported": "SQLiteSession" in agents.__all__,
        }
    )
)
"""
    )

    assert payload["agent_name"] == "Agent"
    assert payload["runner_name"] == "Runner"
    assert payload["session_name"] == "Session"
    assert payload["settings_name"] == "SessionSettings"
    assert payload["sqlite3_loaded"] is False
    assert payload["private_sqlite3_loaded"] is False
    assert payload["sqlite_session_loaded"] is False
    assert payload["sqlite_session_exported"] is True


def test_sqlite_session_top_level_export_is_lazy() -> None:
    payload = _run_python(
        """
import json
import sys

import agents

loaded_after_import = "agents.memory.sqlite_session" in sys.modules

from agents import SQLiteSession

loaded_after_export = "agents.memory.sqlite_session" in sys.modules

print(
    json.dumps(
        {
            "sqlite_session_name": SQLiteSession.__name__,
            "loaded_after_import": loaded_after_import,
            "loaded_after_export": loaded_after_export,
            "sqlite3_loaded": "sqlite3" in sys.modules,
        }
    )
)
"""
    )

    assert payload["sqlite_session_name"] == "SQLiteSession"
    assert payload["loaded_after_import"] is False
    assert payload["loaded_after_export"] is True
    assert payload["sqlite3_loaded"] is True


def test_get_trace_provider_lazily_initializes_defaults() -> None:
    payload = _run_python(
        """
import json

from agents.tracing import setup as tracing_setup
from agents.tracing import processors as tracing_processors

provider_before = tracing_setup.GLOBAL_TRACE_PROVIDER
exporter_before = tracing_processors._global_exporter
processor_before = tracing_processors._global_processor
shutdown_before = tracing_setup._SHUTDOWN_HANDLER_REGISTERED

provider = tracing_setup.get_trace_provider()

provider_after = tracing_setup.GLOBAL_TRACE_PROVIDER
exporter_after = tracing_processors._global_exporter
processor_after = tracing_processors._global_processor
shutdown_after = tracing_setup._SHUTDOWN_HANDLER_REGISTERED

print(
    json.dumps(
        {
            "provider_before": provider_before is not None,
            "exporter_before": exporter_before is not None,
            "processor_before": processor_before is not None,
            "shutdown_before": shutdown_before,
            "provider_after": provider_after is not None,
            "exporter_after": exporter_after is not None,
            "processor_after": processor_after is not None,
            "shutdown_after": shutdown_after,
            "provider_matches_global": provider_after is provider,
        }
    )
)
"""
    )

    assert payload["provider_before"] is False
    assert payload["exporter_before"] is False
    assert payload["processor_before"] is False
    assert payload["shutdown_before"] is False

    assert payload["provider_after"] is True
    assert payload["exporter_after"] is True
    assert payload["processor_after"] is True
    assert payload["shutdown_after"] is True
    assert payload["provider_matches_global"] is True


def test_get_trace_provider_bootstraps_once() -> None:
    payload = _run_python(
        """
import json

from agents.tracing import processors as tracing_processors
from agents.tracing import setup as tracing_setup

registrations = []

def fake_register(fn):
    registrations.append(fn)
    return fn

tracing_setup.atexit.register = fake_register
tracing_setup.GLOBAL_TRACE_PROVIDER = None
tracing_setup._SHUTDOWN_HANDLER_REGISTERED = False
tracing_processors._global_exporter = None
tracing_processors._global_processor = None

first = tracing_setup.get_trace_provider()
second = tracing_setup.get_trace_provider()

print(
    json.dumps(
        {
            "same_provider": first is second,
            "shutdown_registration_count": sum(
                1
                for fn in registrations
                if getattr(fn, "__name__", "") == "_shutdown_global_trace_provider"
            ),
            "provider_initialized": tracing_setup.GLOBAL_TRACE_PROVIDER is not None,
            "exporter_initialized": tracing_processors._global_exporter is not None,
            "processor_initialized": tracing_processors._global_processor is not None,
        }
    )
)
"""
    )

    assert payload["same_provider"] is True
    assert payload["shutdown_registration_count"] == 1
    assert payload["provider_initialized"] is True
    assert payload["exporter_initialized"] is True
    assert payload["processor_initialized"] is True


def test_set_trace_provider_skips_default_bootstrap() -> None:
    payload = _run_python(
        """
import json

from agents.tracing import processors as tracing_processors
from agents.tracing import setup as tracing_setup
from agents.tracing.provider import DefaultTraceProvider

registrations = []

def fake_register(fn):
    registrations.append(fn)
    return fn

tracing_setup.atexit.register = fake_register
tracing_setup.GLOBAL_TRACE_PROVIDER = None
tracing_setup._SHUTDOWN_HANDLER_REGISTERED = False
tracing_processors._global_exporter = None
tracing_processors._global_processor = None

custom_provider = DefaultTraceProvider()
tracing_setup.set_trace_provider(custom_provider)
retrieved_provider = tracing_setup.get_trace_provider()

print(
    json.dumps(
        {
            "custom_provider_returned": retrieved_provider is custom_provider,
            "shutdown_registration_count": sum(
                1
                for fn in registrations
                if getattr(fn, "__name__", "") == "_shutdown_global_trace_provider"
            ),
            "exporter_initialized": tracing_processors._global_exporter is not None,
            "processor_initialized": tracing_processors._global_processor is not None,
        }
    )
)
"""
    )

    assert payload["custom_provider_returned"] is True
    assert payload["shutdown_registration_count"] == 1
    assert payload["exporter_initialized"] is False
    assert payload["processor_initialized"] is False
