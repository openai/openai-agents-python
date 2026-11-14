"""Native child role configuration files."""

from __future__ import annotations

import json as json
from pathlib import Path as Path
from tempfile import TemporaryDirectory as TemporaryDirectory
from typing import Any as Any

from . import tools as _tool_types


def _configure_child_roles(
    config: dict[str, Any],
    home: TemporaryDirectory[str],
    native_agents: _tool_types.NativeAgents[Any],
) -> None:
    for role in native_agents.additional_roles:
        role_path = Path(home.name) / ("role-" + role.name + ".toml")
        role_fields = {"developer_instructions": role.instructions}
        if role.model is not None:
            role_fields["model"] = role.model
        if role.model_settings and "reasoning_effort" in role.model_settings:
            role_fields["model_reasoning_effort"] = role.model_settings["reasoning_effort"]
        role_path.write_text(
            "".join(
                key + " = " + json.dumps(value, ensure_ascii=False) + "\n"
                for key, value in role_fields.items()
            ),
            encoding="utf-8",
        )
        config["agents." + role.name] = {
            "description": role.description,
            "config_file": str(role_path),
        }
