# Contributor Guide

## Table of Contents

1. [Policies & Mandatory Rules](#policies--mandatory-rules)
2. [Code Review Rules](#code-review-rules)
3. [Project Structure Guide](#project-structure-guide)
4. [Operation Guide](#operation-guide)

## Policies & Mandatory Rules

### Mandatory Skill Usage

Repository skills are stored under `.agents/skills/`. References below authorize their use when the stated condition applies; no separate manual invocation is needed unless explicitly required. Read the selected `SKILL.md`, then only the supporting references needed for its route. User instructions and already-approved scope take precedence over skill defaults, subject to applicable permissions. Do not repeat an approval already given for local implementation, review, or verification.

- **`$implementation-strategy`:** Use before changing or reviewing SDK runtime behavior, public APIs, configuration, persisted schemas, or wire protocols. Record required behavior, compatibility, unsupported cases, and an existing alternative in a short scope contract. Revisit it only when feedback changes the contract or implementation shape. Independent reviewers inherit that contract and report uncertainty instead of rerunning strategy.
- **`$implementation-final-review`:** After focused checks, use for runtime code, tests, examples, build/test behavior, and behavior-impacting docs. Its entrypoint owns the lightweight/ordinary/high-risk classification: behavior changes normally require independent review; only demonstrably non-semantic changes can omit it. Finish required review before broad final checks. Planning, investigation, and report-only tasks do not invoke this workflow. Repo-meta changes use applicable skill validation; implementing changes to decision-making guidance requires realistic scenario checks and an independent pass. Report-only assessments can use existing evidence without starting an implementation review.
- **`$code-change-verification`:** Run the final SDK stack for changes to `src/agents/`, `tests/`, `examples/`, shared runtime utilities, or SDK build/test configuration such as `pyproject.toml`, `Makefile`, `mkdocs.yml`, `docs/scripts/`, and CI workflows. Docs-only and repo-meta changes can skip it unless they affect those build/test paths or the user requests the full stack. Lightweight review does not waive eligible SDK checks. The skill owns command order, sandbox execution, host-capacity checks, and retry rules.
- **`$openai-knowledge`:** Use when OpenAI API/platform behavior needs authoritative external evidence. Inspect local code for SDK-owned behavior; do not repeat unchanged external research for purely local implementation details.
- **`$pr-draft-summary`:** After applicable review and verification, generate the local PR draft for runtime, tests, examples, build/test changes, or behavior-impacting docs, including uncommitted work. Skip repo-meta/editorial-only work, an explicit user opt-out, or the release-specific handoff below. A draft never authorizes a branch, commit, push, or PR creation.
- **`$release-candidate-prep`:** Use only when explicitly invoked with a version. Follow its dedicated-worktree workflow and `$final-release-review` gate; its complete final-candidate report replaces the general PR draft. All runtime/docs changes must already be on `main`. The release commit contains only `pyproject.toml`, `uv.lock`, and `tests/fixtures/released_api_contract.json`. See [.github/RELEASING.md](.github/RELEASING.md) for maintainer release operations.

Continue authorized local work through fixes, applicable review, verification, and handoff. Stop for a concrete unresolved contract or scope decision, missing authority, or an external blocker. When a skill causes a stop, identify the exact instruction and explain the missing decision; do not ask for a generic continuation prompt. Never push, open a PR, or otherwise mutate GitHub.

### Work Status Reporting

- Use `RUNNING` only in commentary while autonomous work remains and no user action is required. Do not end a turn with a final response that says the task is still running or asks the user to send a generic continuation prompt.
- Use `COMPLETE` in the final response only when the requested work and every applicable review, verification, and local handoff step are complete.
- Use `NEEDS_DECISION` in the final response only when progress requires a concrete user choice, expanded authority, or an unresolved external condition. State the exact decision or condition instead of asking the user to say "continue".

### Git Worktree and Branch Safety

Work in the user's current checkout and on the current branch by default. If the Codex task is already running in a selected Git worktree, use that worktree without requesting additional permission. Do not create or switch to another Git worktree, and do not create or switch branches, unless the user explicitly asks for or approves that exact action in the current conversation. A request to implement, investigate, review, test, or verify changes does not by itself authorize changing the active worktree or branch.

If isolation or a different checkout is needed, explain why and ask the user before changing Git state. This requirement also applies when another rule or workflow recommends a linked worktree: stop and request approval instead of choosing or creating one automatically.

### Documentation Release Timing

When a feature or bug fix introduces behavior that is not yet available in the latest published release, do not include `docs/` changes that describe that unreleased behavior in the feature or bug-fix pull request, and do not expect those changes as part of that pull request. Handle them in a separate docs-only pull request so maintainers can coordinate its merge timing with the release that makes the documentation accurate. This exception applies only when the documentation would be incorrect for the latest published release; documentation that is already accurate for released behavior remains part of the normal change scope.

Determine whether documentation is required separately from deciding which pull request should carry it. When required `docs/` content would describe behavior that is not available in the latest published release, classify it as separately timed documentation work rather than a missing deliverable or blocking finding for the feature or bug-fix pull request. This timing rule takes precedence over general documentation-completeness requirements in code-review rules, pull-request guidance, and repository skills. It applies to `docs/` content, not automatically to examples or code-level documentation that ships with the changed API.

### Documentation Verification Tiers

Classify documentation changes before choosing review and verification work. Use the narrowest tier that covers the complete diff, and move to a higher tier when any changed file or claim requires it.

- **Editorial:** Terminology, spelling, punctuation, formatting, or link-label changes that do not change documented behavior, runnable code, navigation, link targets, anchors, or generated reference content. Inspect the diff, run targeted searches for the corrected text, and run `git diff --check`. Check a link or anchor directly only when the edit can affect it. Skip `$implementation-final-review`, cross-language review, and `make build-docs` for this tier.
- **Content:** New or materially rewritten behavioral guidance, migration instructions, or runnable snippets that do not change documentation structure or tooling. Verify claims against the implementation and authoritative sources, execute or otherwise validate changed snippets when practical, perform the required focused cross-language review, and run `make build-docs` once after the content and review are stable. Do not repeat the full site build after edits that cannot affect its result.
- **Structural:** Added, removed, renamed, or moved pages; changes to `mkdocs.yml`, generated API reference inputs, documentation scripts, plugins, or build configuration. Run the relevant generators or focused tooling checks and `make build-docs` after the structure is stable. Apply `$code-change-verification` when the changed file is build or test configuration covered by that skill.

Existing warnings from a successful documentation build are not findings for an unrelated docs change. Evaluate the exit status and identify new errors, broken references, or warnings caused by the diff instead of reviewing the complete warning stream line by line. Reserve `make build-full-docs` and generated translation output for translation-tooling changes, explicit localization work, or a specifically requested broad localization audit.

### Scope Discipline and Complexity Reset

Implement the smallest supported behavior requested. Reuse existing sources of truth; every new abstraction, state field, compatibility branch, or test permutation needs a requirement, released contract, durable boundary, or verified risk. Constructibility and a released-version reproducer alone do not establish support.

When related findings repeatedly expand the same design, stop adding conditions, group root causes, and reassess the complete diff against the original requirement. A second related finding that adds another compatibility case or protocol hop triggers this reset. Prefer deleting unsupported branch-local machinery or rejecting unsupported inputs with an existing alternative. Follow `$implementation-strategy` for the detailed reset procedure; preserve released contracts and unrelated user changes.

### ExecPlans

Call out compatibility risk early in your plan only when the change affects behavior shipped in the latest release tag or a released or explicitly supported durable external state boundary, and confirm the approach before implementing changes that could impact users.

Use an ExecPlan when work is multi-step, spans several files, involves new features or refactors, or is likely to take more than about an hour. Start with the template and rules in `PLANS.md`, keep milestones and living sections (Progress, Surprises & Discoveries, Decision Log, Outcomes & Retrospective) up to date as you execute, and rewrite the plan if scope shifts. Call out compatibility risk only when the plan changes behavior shipped in the latest release tag or a released or explicitly supported durable external state boundary. Do not treat branch-local interface churn or unreleased post-tag changes on `main` as breaking by default; prefer direct replacement over compatibility layers in those cases, and renumber or squash unreleased persisted schemas before release when the intermediate snapshots are intentionally unsupported. If you intentionally skip an ExecPlan for a complex task, note why in your response so reviewers understand the choice.

### Public API Compatibility

Treat the parameter and dataclass field order of exported runtime APIs as a compatibility contract.

- For public constructors (for example `RunConfig`, `FunctionTool`, `AgentHookContext`), preserve existing positional argument meaning. Do not insert new constructor parameters or dataclass fields in the middle of existing public order.
- When adding a new optional public field/parameter, append it to the end whenever possible and keep old fields in the same order.
- If reordering is unavoidable, add an explicit compatibility layer and regression tests that exercise the old positional call pattern.
- Prefer keyword arguments at call sites to reduce accidental breakage, but do not rely on this to justify breaking positional compatibility for public APIs.
- Treat intended import paths and `__all__` membership as compatibility contracts. When adding or moving a public symbol, update the owning module, intended top-level or subpackage re-exports, and an import regression test. Keep top-level imports free of optional-dependency failures and runtime side effects; use lazy exports when needed.

### Platform, Docs, and Security Review

- Treat translation-safe English as a documentation compatibility requirement. In new or materially rewritten translatable prose under `docs/` (excluding generated API reference pages), state the actor, scope, ownership, ordering, modality, and lifecycle boundary explicitly whenever they affect the meaning. Use exact API identifiers in inline code, and replace ambiguous pronouns, overloaded nouns, or shorthand when a small clarification can prevent a materially different translation. Do not change the documented behavior merely to make a sentence easier to translate.
- For new or materially rewritten translatable prose, use a lightweight cross-language review of only the changed English sentences and their immediate context. Have an independent reviewer or review pass inspect the source from Japanese, Korean, and Chinese translation perspectives and report only concrete risks such as an ambiguous actor, scope, ownership, ordering, modality, lifecycle boundary, overloaded SDK term, or identifier corruption. Resolve concrete findings in the English source and review the revised lines once. Do not generate full localized pages for routine documentation changes. Pure link, formatting, typo, and other edits that do not change translatable meaning may skip this review.
- If a concrete concern cannot be resolved confidently from the English source, use a temporary translation of only the disputed sentence or paragraph as a focused probe; do not write or commit generated localized files. Reserve `docs/scripts/translate_docs.py --mode full --file <path>` and broader Japanese, Korean, and Chinese output review for changes to the translation tooling or translation controls, explicit localization work, or an explicitly requested broad translation audit. Add or change a fixed translation mapping only when actual cross-document evidence shows that one stable target term is correct across contexts. Prefer contextual guidance and established target-language developer terminology, including standard English terms, over a large or rigid mapping table.
- Treat runnable docs snippets as API compatibility checks. Before adding OpenAI API, provider, Responses, Realtime, WebSocket, or SDK constructor examples, verify the shown arguments and call shape against the actual implementation.
- When adding or updating code in `examples/` or runnable `docs/` snippets, import Agents SDK decorators from `agents.decorators`. Prefer `tool` over `function_tool`; keep non-decorator SDK imports on their existing public import paths.
- Do not let untrusted sandbox manifests opt themselves out of host filesystem or base-directory boundaries. Escape hatches for local source materialization must be controlled by trusted application code at the call site, not by serialized manifest data.
- When documenting sandbox or security grants, verify the actual implementation path enforces the grant or boundary. Do not claim a grant applies to `LocalDir`, `LocalFile`, archive extraction, or other materialization paths unless those paths actually consult it.
- When redacting OpenAI tool, MCP, model, or provider payloads, consider traceback display, exception chaining, `__context__`, logs, and telemetry. Suppressing display with `raise ... from None` is not enough if the original exception object still carries sensitive input data.
- For OpenAI platform or SDK-specific docs changes, prefer `$openai-knowledge` for authoritative platform behavior and inspect the local code path for SDK behavior. Do not rely on generic API assumptions when documenting Responses, Chat Completions, Realtime, tools, MCP, or provider adapters.
- For Realtime tracing changes, read [Realtime tracing architecture](.agents/references/realtime-tracing.md) before proposing SDK spans. Realtime API server traces and Agents SDK client traces are separate; `group_id` can correlate them but does not create a shared trace hierarchy.

## Code Review Rules

### Finding threshold and supported scope

- Report a runtime defect only when the changed code causes a concrete incorrect behavior on a supported path. State the triggering scenario and the caller-visible, compatibility, security, persistence, or lifecycle consequence; omit the finding when no such consequence can be established.
- Treat added abstractions, state, validation, compatibility handling, fallback behavior, dependencies, or parallel paths as actionable only when the machinery does not map to the task, a released contract, supported durable state, or a verified runtime or platform risk. Identify the exact unnecessary machinery and recommend the smallest safe removal or direct replacement.
- Flag runtime validation, compatibility handling, fallback behavior, or tests added only for synthetic or unsupported values when no ordinary supported producer, released contract, durable boundary, or actual untrusted-input path can produce the value with a concrete consequence. Constructibility in Python, manually corrupted typed objects, monkeypatched state, and direct helper calls that bypass the owning public or wire boundary are not sufficient justification. This includes non-finite or extreme numbers and impossible enum or discriminated-union members unless the exact category is intentionally supported.
- Do not duplicate client-side runtime validation solely for values already excluded by the public type contract or authoritatively rejected by the upstream provider. Add fail-fast SDK validation only when delayed rejection creates a concrete SDK-owned problem before the authoritative rejection, such as an irreversible side effect, persistent corruption, security or privacy exposure, avoidable billable work, repeated resource consumption, or an error that arrives too late or is too opaque for reasonable correction. A security label alone is insufficient without a complete trace from attacker-controlled input through an actual trust boundary to the protected outcome.
- Do not report a defect merely because another semantic choice appears cleaner, more symmetric, or easier to explain. When repository evidence does not select one contract, report only a concrete inconsistency with an already supported path or an established caller-visible expectation.
- Flag a new public option, callback, class, compatibility branch, or parallel execution path when the exact required outcome, including its lifecycle and compatibility constraints, is already available through a reasonable supported API or composition path. Name that path and recommend removal or narrower reuse of the existing source of truth.
- Report compatibility findings only against behavior shipped in the latest release, an explicitly supported public contract, or a durable external state or protocol boundary. Do not require compatibility shims for unreleased branch-local helpers, same-branch tests, or intermediate persisted formats that are intentionally unsupported.

### Contract and lifecycle coverage

- For every added or modified public field, configuration value, event, serialized value, or wire value, inspect all supported construction, forwarding, adapter, and consumption paths. Flag partial implementations where normal, specialized, default, missing-value, or error paths silently drop, reshape, or reject the value inconsistently. Include intended public imports and generated package surfaces when they are part of the changed contract.
- Require parity across streaming and non-streaming, sync and async, initial and resumed, direct and wrapped, or provider-specific paths only when the accepted requirement or existing contract covers those paths. Do not report missing parity solely for API symmetry or conceptual similarity.
- When changed code mutates shared state across an `await`, callback, retry, reconnect, cancellation, cleanup, or rollback boundary, check whether stale or failing work can overwrite, revert, or dispose state owned by surviving work. Report the concrete interleaving and the missing ownership, generation, identity, transaction, revalidation, or serialization invariant at the actual mutation boundary; sequential happy-path tests are insufficient.
- When a new validation or failure path can run after resources or observable state are acquired, verify cleanup explicitly and preserve the primary failure. Report concrete leaked resources, stale state, lost handlers, or survivor corruption rather than assuming normal teardown runs after failed construction or context entry.
- Flag persisted, resumed, serialized, provider-controlled, or manifest data that is treated as authority for a host-owned runtime, security, identity, or cleanup decision unless the supported trust boundary explicitly grants that authority. Preserve trusted current configuration and validate untrusted state before it can affect side effects, replay, or resource ownership.

### Test and documentation evidence

- Treat tests as contract evidence only when they exercise the highest stable caller-visible boundary that controls the observable result and derive expected behavior from the requirement, released behavior, a worked example, a baseline, or another independent oracle. Do not accept helper-only call-shape assertions or expected values recomputed with the implementation's own logic when another layer owns the outcome.
- Require representative regression coverage for the accepted behavior and intentionally unsupported category. For concurrency findings, require controlled completion ordering plus assertions about the surviving operation and final shared state. Do not request exhaustive tests for every constructible permutation.
- Report missing documentation or examples only when the patch makes existing guidance materially false, unsafe, or misleading; correct use depends on a non-obvious migration, compatibility boundary, constraint, or operational warning; or the accepted feature would otherwise be practically unusable. Do not report optional completeness or discoverability improvements as blocking findings.
- Decide `docs/` delivery timing separately from documentation necessity. If required `docs/` content would describe behavior unavailable in the latest published release, apply [Documentation Release Timing](#documentation-release-timing): record it as separately timed work, and do not report its absence as a blocking finding for the feature or bug-fix pull request. This exception does not automatically defer examples or code-level documentation that ships with the changed API.
- Do not report formatting, lint, full-suite status, commit history, or pull-request description quality as code findings; those are CI or repository-readiness conditions.

### Review scope

- Review the complete diff from the merge base of the intended target branch, or from the latest release tag when it is the compatibility baseline, not only the latest incremental fix. Passing tests do not justify branch-local machinery that no longer matches the original requirement.
- Keep findings scoped to consequences introduced, exposed, or worsened by the patch. Do not block on unrelated cleanup, pre-existing bugs, optional refactors, or speculative extensibility merely discovered while reading adjacent code. A pre-existing condition is in scope when the patch newly reaches it on a supported path, relies on it for correctness, or otherwise makes its consequence part of the changed behavior.
- Require a broader refactor only when concrete evidence shows the focused change would otherwise remain incorrect, unsafe, incompatible, or dependent on duplicated sources of truth that can observably diverge.

## Project Structure Guide

### Overview

The OpenAI Agents Python repository provides the Python Agents SDK, examples, and documentation built with MkDocs. Use `uv run python ...` for Python commands to ensure a consistent environment.

### Repo Structure & Important Files

- `src/agents/`: Core library implementation.
- `tests/`: Test suite; see `tests/README.md` for snapshot guidance.
- `examples/`: Sample projects showing SDK usage.
- `docs/`: MkDocs documentation source; do not edit translated docs under `docs/ja`, `docs/ko`, or `docs/zh` (they are generated).
- `docs/scripts/`: Documentation utilities, including translation and reference generation.
- `mkdocs.yml`: Documentation site configuration.
- `Makefile`: Common developer commands.
- `pyproject.toml`, `uv.lock`: Python dependencies and tool configuration.
- `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`: Pull request template to use when opening PRs.
- `.agents/references/`: Durable SDK maintainer architecture references. Start with [the reference map](.agents/references/README.md) and open only the files relevant to the affected runtime boundary.
- `site/`: Built documentation output.

### Agents Core Runtime Guidelines

- For `Agent` fields, cloning, dynamic instructions, enabled tools or handoffs, output schemas, run context wrappers, usage aggregation, or public-versus-internal agent identity, read [Agent definition and run context](.agents/references/agent-definition-and-run-context.md).
- `src/agents/run.py` is the runtime entrypoint (`Runner`, `AgentRunner`). Keep it focused on orchestration and public flow control. Put new runtime logic under `src/agents/run_internal/` and import it into `run.py`.
- When `run.py` grows, refactor helpers into `run_internal/` modules (for example `run_loop.py`, `turn_resolution.py`, `tool_execution.py`, `session_persistence.py`) and leave only wiring and composition in `run.py`.
- For turn accounting, guardrail ordering, handoffs, interruptions, cancellation, hooks, or streaming behavior, read [Runner lifecycle](.agents/references/runner-lifecycle.md). Keep streaming and non-streaming paths behaviorally aligned.
- For new model output, tool call, approval, or run item variants, read [Run item lifecycle](.agents/references/run-item-lifecycle.md) and update every applicable processing, event, replay, persistence, tracing, and serialization surface.
- For function-tool parameter schemas, `Annotated` or `Field` metadata, strict JSON schema conversion, or structured output schemas, read [Function and output schema](.agents/references/function-and-output-schema.md).
- For function-tool naming, namespacing, lookup, approvals, tracing, or call-ID changes, read [Tool identity and routing](.agents/references/tool-identity.md) and use the canonical helpers in `src/agents/_tool_identity.py` instead of adding local normalization rules.
- For function-tool planning, approval ordering, tool guardrails, concurrency, cancellation, timeouts, hooks, or failure conversion, read [Tool execution lifecycle](.agents/references/tool-execution-lifecycle.md).
- For local MCP connection ownership, `MCPServerManager`, request serialization, tool caching or filtering, transport retries, cancellation, or cleanup, read [Local MCP server lifecycle](.agents/references/local-mcp-server-lifecycle.md).
- For trace or span context, processors, export, flush, shutdown, sensitive data, or resumed trace state, read [Tracing lifecycle](.agents/references/tracing-lifecycle.md).
- For `RealtimeSession` lifecycle, background-task, handoff, listener, connection, or cleanup changes, read [Realtime session lifecycle](.agents/references/realtime-session-lifecycle.md) and verify both normal and failure-path resource ownership.
- For `VoicePipeline`, streamed audio input, STT session ownership, TTS task ordering, voice lifecycle events, PCM framing, or voice tracing changes, read [Voice pipeline lifecycle](.agents/references/voice-pipeline-lifecycle.md).
- For server-managed conversation (`conversation_id`, `previous_response_id`, `auto_previous_response_id`), read [Conversation state ownership](.agents/references/conversation-state-ownership.md) before changing continuation, filtering, retry, compaction, handoffs, or resume behavior.
- For client-managed session input, per-turn saves, retry rewind, backend atomicity, or compaction replacement, read [Session persistence](.agents/references/session-persistence.md).
- For model resolution, `ModelSettings`, provider adapters, Responses versus Chat Completions capabilities, request conversion, terminal events, transport reuse, or model retries, read [Model and provider boundaries](.agents/references/model-provider-boundaries.md).
- If the serialized `RunState` shape changes, read [RunState schema and resume boundary](.agents/references/runstate-schema.md) and follow its release-boundary, schema-version, backward-read, and regression-test rules.
- For sandbox session ownership, agent preparation, manifests, host-path materialization, snapshots, resume state, or cleanup, read [Sandbox runtime boundary](.agents/references/sandbox-runtime-boundary.md).

## Operation Guide

### Prerequisites

- Python 3.10+.
- `uv` installed for dependency management (`uv sync`) and `uv run` for Python commands.
- `make` available to run repository tasks.

### Development Workflow

Stay in the current checkout and branch unless a Git state change is explicitly authorized. Install dependencies with `make sync` for a fresh checkout or changed dependencies. Implement the requested behavior, add meaningful regression coverage, and follow the applicable skills above through final handoff. Commit only when authorized; keep commit messages concise and imperative. GitHub actions remain outside this workflow's authority.

### Testing & Automated Checks

Use focused checks during iteration. Add tests for required behavior and concrete regressions, not to mirror implementation logic. Reuse passing checks for unchanged content; broaden or repeat them only when changes, failures, or unresolved concerns justify it. Run eligible final SDK verification after clean review and documentation checks according to their tiers.

For provider-neutral agent workflow tests, prefer `ScriptedModel` from `agents.testing` over adding a new mock or fake `Model`. Prefer `ScriptedRealtimeModel` from `agents.realtime.testing` for Realtime session tests, the scripted utilities from `agents.voice.testing` for Voice pipeline tests, and `scripted_sandbox_session()` from `agents.testing` for deterministic Sandbox session calls. Keep a specialized test double only when the test specifically requires provider-wire conversion, malformed streams, controlled suspension or concurrency, or an exact cancellation or lifecycle boundary that the scripted utilities cannot preserve; document that boundary in the test.

Before adding or changing async, retry, timeout, subprocess, PTY, warning, or xdist-sensitive tests, read [Performance and determinism](tests/README.md#performance-and-determinism) and preserve the applicable behavioral and lifecycle coverage while optimizing execution.

For test execution, coverage, and snapshot workflows, use [tests/README.md](tests/README.md) and the `Makefile`. `$code-change-verification` owns the required final command sequence. Keep Python comments as full sentences ending with a period.

- Do not hard-wrap prose in Markdown or other non-code text files at a fixed column width. Keep each paragraph on one source line unless the file format or Markdown structure requires a line break, such as for lists, tables, blockquotes, or code fences.

### Pull Request & Commit Guidelines

- Use the template at `.github/PULL_REQUEST_TEMPLATE/pull_request_template.md`; include a summary, test plan, and issue number if applicable.
- In copy-ready GitHub text, use native issue and pull-request references: exactly `#123` for this repository and `owner/repo#123` for another repository. Do not qualify same-repository references as `openai/openai-agents-python#123`. Preserve closing forms such as `Fixes #123` or `Resolves #123`. Never wrap these references in Markdown links such as `[PR #123](https://github.com/owner/repo/pull/123)` or `[#123](...)`; those Codex-friendly links require manual cleanup after pasting into GitHub. Use descriptive Markdown links only for external resources or GitHub targets that cannot be expressed as a native issue or pull-request reference.
- Add focused regression tests for accepted new behavior when feasible. Update documentation or examples when the change would otherwise make existing guidance materially false, unsafe, or misleading; correct use depends on a non-obvious constraint, migration step, compatibility boundary, or operational warning; or the accepted feature would otherwise be practically unusable. Do not require optional documentation or examples solely for completeness.
- Determine `docs/` delivery timing separately from documentation necessity. When required `docs/` content would describe behavior that is not yet in the latest published release, leave it out of the feature or bug-fix pull request and treat it as separately timed docs-only work, not as an incomplete current pull request. This exception does not automatically apply to examples or code-level documentation that ships with the changed API.
- Commit messages should be concise and written in the imperative mood. Small, focused commits are preferred.
