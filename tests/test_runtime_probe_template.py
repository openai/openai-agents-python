"""Check probe metadata against the distribution installed for these tests."""

import runpy
from importlib import metadata
from pathlib import Path


def test_runtime_context_reports_installed_sdk_distribution() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / ".agents/skills/runtime-behavior-probe/templates/python_probe.py"
    )
    probe = runpy.run_path(str(template))
    packages = probe["runtime_context"]()["package_versions"]
    assert packages["openai-agents"] == metadata.version("openai-agents")
    assert "agents" not in packages
