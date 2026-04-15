# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.
"""

from temporalio.contrib.openai_agents._mcp import (
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
)
from temporalio.contrib.openai_agents._model_parameters import ModelActivityParameters
from temporalio.contrib.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
)
from temporalio.contrib.openai_agents.sandbox._sandbox_client_provider import (
    SandboxClientProvider,
)
from temporalio.contrib.openai_agents.workflow import AgentsWorkflowError

from . import testing, workflow

__all__ = [
    "AgentsWorkflowError",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIPayloadConverter",
    "SandboxClientProvider",
    "StatelessMCPServerProvider",
    "StatefulMCPServerProvider",
    "testing",
    "workflow",
]
