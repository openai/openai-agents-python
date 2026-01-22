# PR auto-labeling

You are Codex running in CI to propose labels for a pull request in the openai-agents-python repository.

Inputs:
- PR diff: .tmp/pr-labels/changes.diff
- Changed files: .tmp/pr-labels/changed-files.txt

Task:
- Inspect the diff and changed files.
- Output JSON with a single top-level key: "labels" (array of strings).
- Include every label that applies. Only use labels from the allowed list.

Allowed labels:
- documentation
- project
- bug
- enhancement
- dependencies
- feature:chat-completions
- feature:core
- feature:lite-llm
- feature:mcp
- feature:realtime
- feature:sessions
- feature:tracing
- feature:voice

Label rules:
- documentation: Documentation changes (docs/), or src/ changes that only modify comments/docstrings without behavior changes. If only comments/docstrings change in src/, do not add bug/enhancement.
- project: Any change to pyproject.toml.
- dependencies: Dependencies are added/removed/updated (pyproject.toml dependency sections or uv.lock changes).
- bug: src/ code changes that fix incorrect behavior.
- enhancement: src/ code changes that add or expand functionality.
- feature:chat-completions: Changes related to Chat Completions integration or conversion, including the litellm chat completions converter.
- feature:core: Core agent loop, tool calls, run pipeline, or other central runtime behavior. Do not use this if the changes are only in other feature areas (extensions, mcp, etc.). If both core and other areas changed, include both.
- feature:lite-llm: Litellm adapter/provider changes.
- feature:mcp: MCP integration changes.
- feature:realtime: Realtime agent changes.
- feature:sessions: Sessions/memory handling changes.
- feature:tracing: Tracing feature changes.
- feature:voice: Voice pipeline (stt -> llm -> tts) or related components.

Output:
- JSON only (no code fences, no extra text).
- Example: {"labels":["enhancement","feature:core"]}
