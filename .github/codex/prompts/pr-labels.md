# PR auto-labeling

You are Codex running in CI to propose labels for a pull request in the openai-agents-python repository.

Inputs:
- PR context: .tmp/pr-labels/pr-context.json
- PR diff: .tmp/pr-labels/changes.diff
- Changed files: .tmp/pr-labels/changed-files.txt

Task:
- Inspect the PR context, diff, and changed files.
- Output JSON with a single top-level key: "labels" (array of strings).
- Only use labels from the allowed list.
- Prefer false negatives over false positives. If you are unsure, leave the label out.
- Return the smallest accurate set of labels for the PR's primary intent and primary surface area.

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

Important guidance:
- `documentation`, `project`, and `dependencies` are also derived deterministically elsewhere in the workflow. You may include them when the evidence is explicit, but do not stretch to infer them from weak signals.
- Use direct evidence from changed implementation files and the dominant intent of the diff. Do not add labels based only on tests, examples, comments, docstrings, imports, type plumbing, or shared helpers.
- Cross-cutting features often touch many adapters and support layers. Only add a `feature:*` label when that area is itself a primary user-facing surface of the PR, not when it receives incidental compatibility or parity updates.
- Mentions of a feature area in helper names, comments, tests, or trace metadata are not enough by themselves.
- Prefer the most general accurate feature label over a larger set of narrower labels. For broad runtime work, this usually means `feature:core`.
- A secondary `feature:*` label needs two things: a non-test implementation/docs change in that area, and evidence that the area is a user-facing outcome of the PR rather than support work for another feature.

Label rules:
- documentation: Documentation changes (docs/), or src/ changes that only modify comments/docstrings without behavior changes. If only comments/docstrings change in src/, do not add bug/enhancement.
- project: Any change to pyproject.toml.
- dependencies: Dependencies are added/removed/updated (pyproject.toml dependency sections or uv.lock changes).
- bug: The PR's primary intent is to correct existing incorrect behavior. Use only with strong evidence such as the title/body/tests clearly describing a fix, regression, crash, incorrect output, or restore/preserve behavior. Do not add `bug` for incidental hardening that accompanies a new feature.
- enhancement: The PR's primary intent is to add or expand functionality. Prefer `enhancement` for feature work even if the diff also contains some fixes or guardrails needed to support that feature.
- bug vs enhancement: Prefer exactly one of these. Include both only when the PR clearly contains two separate substantial changes and both are first-order outcomes.
- feature:chat-completions: Chat Completions support or conversion is a primary deliverable of the PR. Do not add it for a small compatibility guard or parity update in `chatcmpl_converter.py`.
- feature:core: Core agent loop, tool calls, run pipeline, or other central runtime behavior is a primary surface of the PR. For cross-cutting runtime changes, this is usually the single best feature label.
- feature:lite-llm: LiteLLM adapter/provider behavior is a primary deliverable of the PR.
- feature:mcp: MCP-specific behavior or APIs are a primary deliverable of the PR. Do not add it for incidental hosted/deferred tool plumbing touched by broader runtime work.
- feature:realtime: Realtime-specific behavior, API shape, or session semantics are a primary deliverable of the PR. Do not add it for small parity updates in realtime adapters.
- feature:sessions: Session or memory behavior is a primary deliverable of the PR. Do not add it for persistence updates that merely support a broader feature.
- feature:tracing: Tracing is a primary deliverable of the PR. Do not add it for trace naming or metadata changes that accompany another feature.
- feature:voice: Voice pipeline behavior is a primary deliverable of the PR.

Decision process:
1. Determine the PR's primary intent in one sentence from the PR title/body and dominant runtime diff.
2. Start with zero labels.
3. Add `bug` or `enhancement` conservatively.
4. Add only the minimum `feature:*` labels needed to describe the primary surface area.
5. Treat extra `feature:*` labels as guilty until proven necessary. Keep them only when the PR would feel mislabeled without them.
6. Re-check every label. Drop any label that is supported only by secondary edits, parity work, or touched files outside the PR's main focus.

Examples:
- If a new cross-cutting runtime feature touches Chat Completions, Realtime, Sessions, MCP, and tracing support code for parity, prefer `["enhancement","feature:core"]` over labeling every touched area.
- If a PR mainly adds a Responses/core capability and touches realtime or sessions files only to keep shared serialization, replay, or adapters in sync, do not add `feature:realtime` or `feature:sessions`.
- If a PR mainly fixes realtime transport behavior and also updates tests/docs, prefer `["bug","feature:realtime"]`.

Output:
- JSON only (no code fences, no extra text).
- Example: {"labels":["enhancement","feature:core"]}
