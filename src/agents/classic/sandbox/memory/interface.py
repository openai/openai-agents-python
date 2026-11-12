from __future__ import annotations

from typing import Any

from _agents_shared.sandbox_memory_artifacts import (
    RolloutExtractionArtifacts as RolloutExtractionArtifacts,
)

ROLLOUT_EXTRACTION_ARTIFACTS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rollout_slug": {"type": "string"},
        "rollout_summary": {"type": "string"},
        "raw_memory": {"type": "string"},
    },
    "required": ["rollout_slug", "rollout_summary", "raw_memory"],
}

ROLLOUT_EXTRACTION_ARTIFACTS_TEXT_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "name": "sandbox_memory_rollout_extraction_artifacts",
    "description": "Sandbox memory rollout extraction artifacts.",
    "schema": ROLLOUT_EXTRACTION_ARTIFACTS_JSON_SCHEMA,
    "strict": True,
}

ROLLOUT_EXTRACTION_ARTIFACTS_TEXT_CONFIG: dict[str, Any] = {
    "format": ROLLOUT_EXTRACTION_ARTIFACTS_TEXT_FORMAT
}
