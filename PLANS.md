# Codex Execution Plans (ExecPlans)

This file defines how to write and maintain an ExecPlan: a self-contained, living specification that a novice can follow to deliver observable, working behavior in this repository.

## When to Use an ExecPlan
- Required for multi-step or multi-file work, new features, refactors, or tasks expected to take more than about an hour.
- Optional for trivial fixes (typos, small docs), but if you skip it for a substantial task, state the reason in your response.

## How to Use This File
- Authoring: read this file end to end before drafting; start from the skeleton; embed all context (paths, commands, definitions) so no external docs are needed.
- Implementing: move directly to the next milestone without asking for next steps; keep the living sections current at every stopping point.
- Discussing: record decisions and rationale inside the plan so work can be resumed later using only the ExecPlan.

## Non-Negotiable Requirements
- Self-contained and beginner-friendly: define every term; include needed repo knowledge; avoid assuming prior plans or external links.
- Living document: revise Progress, Surprises & Discoveries, Decision Log, and Outcomes & Retrospective as work proceeds while keeping the plan self-contained.
- Outcome-focused: describe what the user can do after the change and how to see it working; the plan must lead to demonstrably working behavior, not just code edits.
- Explicit acceptance: state behaviors, commands, and observable outputs that prove success.

## Formatting Rules
- Default envelope is a single fenced code block labeled `md`; do not nest other triple backticks inside—indent commands, transcripts, and diffs instead.
- If the file contains only the ExecPlan, omit the enclosing code fence.
- Use blank lines after headings; prefer prose over lists. Checklists are permitted only in the Progress section (and are mandatory there).

## Guidelines
- Define jargon immediately and tie it to concrete files or commands in this repo.
- Anchor on outcomes: acceptance should be phrased as observable behavior; for internal changes, show tests or scenarios that demonstrate the effect.
- Specify repository context explicitly: full paths, functions, modules, working directory for commands, and environment assumptions.
- Be idempotent and safe: describe retries or rollbacks for risky steps; prefer additive, testable changes.
- Validation is required: state exact test commands and expected outputs; include concise evidence (logs, transcripts, diffs) as indented examples.

## Milestones
- Tell a story (goal → work → result → proof) for each milestone; keep them narrative rather than bureaucratic.
- Each milestone must be independently verifiable and incrementally advance the overall goal.
- Milestones are distinct from Progress: milestones explain the plan; Progress tracks real-time execution.

## Living Sections (must be present and maintained)
- Progress: checkbox list with timestamps; every pause should update what is done and what remains.
- Surprises & Discoveries: unexpected behaviors, performance notes, or bugs with brief evidence.
- Decision Log: each decision with rationale and date/author.
- Outcomes & Retrospective: what was achieved, remaining gaps, and lessons learned.

## Prototyping and Parallel Paths
- Prototypes are encouraged to de-risk changes; keep them additive, clearly labeled, and validated.
- Parallel implementations are acceptable when reducing risk; describe how to validate each path and how to retire one safely.

## ExecPlan Skeleton

```md
# <Short, action-oriented description>

This ExecPlan is a living document. The sections Progress, Surprises & Discoveries, Decision Log, and Outcomes & Retrospective must stay up to date as work proceeds.

If PLANS.md is present in the repo, maintain this document in accordance with it and link back to it by path.

## Purpose / Big Picture
Explain the user-visible behavior gained after this change and how to observe it.

## Progress
- [x] (2025-10-01 13:00Z) Example completed step.
- [ ] Example incomplete step.
- [ ] Example partially completed step (completed: X; remaining: Y).

## Surprises & Discoveries
- Observation: …
  Evidence: …

## Decision Log
- Decision: …
  Rationale: …
  Date/Author: …

## Outcomes & Retrospective
Summarize outcomes, gaps, and lessons learned; compare to the original purpose.

## Context and Orientation
Describe the current state relevant to this task as if the reader knows nothing. Name key files and modules by full path; define any non-obvious terms.

## Plan of Work
Prose description of the sequence of edits and additions. For each edit, name the file and location and what to change.

## Concrete Steps
Exact commands to run (with working directory). Include short expected outputs for comparison.

## Validation and Acceptance
Behavioral acceptance criteria plus test commands and expected results.

## Idempotence and Recovery
How to retry or roll back safely; ensure steps can be rerun without harm.

## Artifacts and Notes
Concise transcripts, diffs, or snippets as indented examples.

## Interfaces and Dependencies
Prescribe libraries, modules, and function signatures that must exist at the end. Use stable names and paths.
```

## Revising a Plan
- When the scope shifts, rewrite affected sections so the document remains coherent and self-contained.
- After significant edits, add a short note at the end explaining what changed and why.
