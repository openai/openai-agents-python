"""Pure MCP name projection shared by declarations and borrowed selection."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from ._public import preserve_module_identity as _preserve_module_identity


def _project_name(raw: str) -> str:
    # Pinned codex-mcp/mcp/mod.rs: sanitize_responses_api_tool_name.
    return "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in raw)


def _project_tool_names(
    servers: Sequence[tuple[str, Sequence[str]]], *, prefix: bool = True
) -> dict[tuple[str, str], dict[str, str]]:
    # Keep this synchronous projection aligned with codex-mcp/src/tools.rs.
    # Declarations and ToolRefs remain inert; Core owns actual peer dispatch.
    candidates: list[dict[str, str]] = []
    for server, tools in servers:
        for raw in (server, *tools):
            if not isinstance(raw, str) or not raw or "\0" in raw:
                raise ValueError("MCP raw identities must be nonempty strings without NUL")
            invalid_unicode = False
            try:
                raw.encode("utf-8")
            except UnicodeError:
                invalid_unicode = True
            if invalid_unicode:
                raise ValueError("MCP raw identities must contain valid Unicode")
        if len(set(tools)) != len(tools):
            raise ValueError("MCP raw tool names must be unique")
        raw_namespace = f"{server}\0{server}\0"
        project = (
            _project_name
            if prefix
            else lambda value: "".join(
                c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in value
            )
        )
        namespace = project(server) or "_"
        if prefix and not namespace.startswith("mcp__"):
            namespace = "mcp__" + namespace
        for raw in tools:
            candidates.append(
                {
                    "server": server,
                    "raw": raw,
                    "namespace": namespace,
                    "name": project(raw) or "_",
                    "raw_namespace": raw_namespace,
                    "identity": f"{raw_namespace}\0{raw}\0{raw}",
                }
            )

    def suffix(identity: str) -> str:
        return "_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]

    namespaces: dict[str, set[str]] = {}
    for candidate in candidates:
        namespaces.setdefault(candidate["namespace"], set()).add(candidate["raw_namespace"])
    for candidate in candidates:
        namespace = candidate["namespace"]
        if len(namespaces[namespace]) > 1:
            hashed = suffix(candidate["raw_namespace"])
            candidate["namespace"] = (
                namespace[:-2] + hashed + "__" if namespace.endswith("__") else namespace + hashed
            )
    leaves: dict[tuple[str, str], set[str]] = {}
    for candidate in candidates:
        leaves.setdefault((candidate["namespace"], candidate["name"]), set()).add(
            candidate["identity"]
        )
    for candidate in candidates:
        if len(leaves[candidate["namespace"], candidate["name"]]) > 1 and (
            prefix or candidate["name"] != candidate["raw"]
        ):
            candidate["name"] += suffix(candidate["identity"])
    projected: dict[tuple[str, str], dict[str, str]] = {}
    used: set[str] = set()
    # Borrowed native names keep their identity before projected siblings reserve names.
    for candidate in sorted(
        candidates,
        key=lambda item: (
            not prefix
            and (
                item["name"] != item["raw"] or len(item["namespace"]) + len(item["name"]) + 2 > 128
            ),
            item["identity"],
        ),
    ):
        namespace, name = candidate["namespace"], candidate["name"]
        if len(namespace) + len(name) + 2 > 128 or namespace + name in used:
            attempt = 0
            while True:
                identity = candidate["identity"]
                hashed = suffix(identity if attempt == 0 else f"{identity}\0{attempt}")
                budget = max(0, 128 - len(candidate["namespace"]) - 2)
                if budget >= len(hashed):
                    namespace = candidate["namespace"]
                    name = candidate["name"][: budget - len(hashed)] + hashed
                else:
                    namespace = candidate["namespace"][: 128 - len(hashed) - 2]
                    name = hashed
                if namespace + name not in used:
                    break
                attempt += 1
        used.add(namespace + name)
        projected[candidate["server"], candidate["raw"]] = {"namespace": namespace, "name": name}
    return projected


_preserve_module_identity(globals(), "agents.mcp")
