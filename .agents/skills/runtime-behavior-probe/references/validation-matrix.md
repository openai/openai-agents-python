# Validation Matrix

Use the matrix to decide what to probe before writing scripts. The goal is not exhaustive combinatorics; the goal is high-value coverage that is visible, explainable, and likely to reveal runtime surprises. The matrix should make the real news easy to scan.

## Minimum Columns

Use these columns unless the task clearly needs more:

- `case_id`: Stable identifier such as `S1`, `E3`, or `R2`.
- `scenario`: Short description of the behavior under test.
- `mode`: `single-shot`, `repeat-N`, or `warm-up + repeat-N`.
- `question`: The concrete runtime uncertainty this case is answering.
- `setup`: Inputs, environment, or preconditions required for the case.
- `observation_summary`: A compact summary of what actually happened.
- `result_flag`: `unexpected`, `negative`, `expected`, or `blocked`.
- `evidence`: Path, log reference, or `deleted`.

Add these columns when they materially improve the investigation:

- `comparison_basis`: The baseline, docs, or prior behavior you are comparing against.
- `variable_under_test`: The single factor that is intentionally changing in a comparison case.
- `held_constant`: Prompt shape, tool setup, model settings, or state rules that were intentionally kept the same.
- `output_constraint`: Any schema, length, or response-shape constraint used to keep the comparison fair.
- `status`: Use `pass`, `fail`, `unexpected-pass`, `unexpected-fail`, or `blocked` only when there is a credible comparison basis or control.
- `confidence`: `high`, `medium`, or `low`.
- `state_setup`: Fresh or reused state, cache strategy, unique IDs, and cleanup checks.
- `repeats`: Number of measured runs.
- `warm_up`: Whether a warm-up run was used and why.
- `variance`: Any useful spread or instability note across repeated runs.
- `usage_note`: Token, usage, or output-length note when it materially affects interpretation.
- `control`: Known-good comparison point for regression or behavior-change questions.
- `risk_profile`: `read-only`, `mutating`, or `costly` for live probes.
- `env_vars`: Exact environment-variable names the case plans to read.
- `approval`: `not-needed`, `pending`, or `approved` for cases that need user permission before execution.

Use `result_flag` as the fast scan field. It is what makes unexpected or negative findings jump out before the reader studies the full report.

Use `status` only when you have a real comparison basis. If the case is exploratory and there is no trustworthy baseline, prefer a strong `observation_summary` plus `result_flag` and `confidence` instead of pretending the result is a clean pass or fail.

## Choosing Execution Mode

Pick an execution mode before you run the case:

- Use `single-shot` for deterministic, one-run checks.
- Use `repeat-N` automatically when the question involves cache behavior, retries, streaming, interruptions, rate limiting, concurrency, or other run-to-run-sensitive behavior.
- Use `warm-up + repeat-N` when the first run is likely to include cold-start effects such as container provisioning, import caches, or prompt-cache population.

Use these defaults unless the task clearly needs something else:

- `repeat-3` for a quick screen of a repeat-sensitive question.
- `warm-up + repeat-10` for decision-grade latency comparisons or release-facing recommendations.
- For costly live probes, start at `repeat-3` and expand only if the answer is still unclear.

If it is genuinely unclear whether extra runs are worth the time or cost, ask the user before expanding the probe.

## Phase The Matrix

When the question is comparative or benchmark-like, do not jump straight to the largest matrix.

Start with a pilot:

- One control.
- One or two highest-signal success cases.
- The smallest repeat count that can disqualify a weak candidate quickly.

Expand only when:

- The candidate survives the pilot.
- The results are close enough that more samples matter.
- A major runtime surface is still uncovered.
- The user explicitly wants decision-grade evidence.

## Coverage Categories

Try to cover at least one case from each relevant category:

- `success`: Normal behavior that should work.
- `control`: Known-good comparison such as `origin/main`, the latest release, or the same request without the suspected option.
- `boundary`: Size, count, or parameter limits near a plausible edge.
- `invalid`: Bad inputs or unsupported combinations.
- `misconfig`: Missing key, wrong endpoint, bad permissions, or incompatible local setup.
- `transient`: Timeout, temporary server issue, network breakage, or rate limiting.
- `recovery`: Retry behavior, partial completion, duplicate submission, or cleanup.
- `concurrency`: Overlapping operations when shared state, ordering, or isolation may matter.
- `quality`: A harder or more open-ended sample when the user is asking about model intelligence, not just workflow parity.

If time is limited, prioritize categories in this order:

1. Known-good control when the question implies regression or drift.
2. Highest-risk success case.
3. Most plausible user-facing failure.
4. Most likely edge case with ambiguous behavior.
5. Cleanup or retry semantics.
6. Lower-probability extremes.

## Matrix Template

Use this compact template:

    | case_id | scenario | mode | question | setup | state_setup | variable_under_test | held_constant | comparison_basis | observation_summary | result_flag | status | evidence |
    | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
    | K1 | Known-good control | single-shot | Does the baseline still show the expected behavior? | Same probe against baseline target | Fresh state | none | current probe shape | `origin/main` or latest release | pending | pending | pending | pending |
    | S1 | Baseline success | single-shot | What does the normal success path look like at runtime? | Valid config and representative input | Fresh state | none | representative input and setup | current docs or local expectation | pending | pending | pending | pending |
    | R1 | Cache or retry behavior | warm-up + repeat-N | Does behavior change after the first run or across retries? | Same request repeated under controlled settings | Cache key or retry setup recorded | reuse versus fresh state | prompt shape and tool setup | same request without reuse, or docs if available | pending | pending | pending | pending |
    | C1 | Model comparison pilot | warm-up + repeat-N | Does candidate B preserve the covered behavior while improving latency? | Same scenario across two models | Fresh state and stable IDs | model name | prompt shape, tool choice, and model settings parity | control model in the same probe | pending | pending | pending | pending |
    | E1 | Invalid input | single-shot | How does the runtime reject a realistic bad input? | Missing required field | Fresh state | invalid field value | same request with valid field | same request with valid field | pending | pending | pending | pending |
    | X1 | Concurrent overlap | repeat-N | Do overlapping runs interfere with each other? | Two or more overlapping operations | Unique IDs plus cleanup verification | overlap timing | same logical input | same request serialized, if available | pending | pending | pending | pending |

## Recording Results

Keep `question` unchanged after execution. Put the actual behavior in `observation_summary`, then mark the scan-friendly `result_flag`.

Use these `result_flag` values consistently:

- `unexpected`: The result diverged from the best current understanding in a surprising way.
- `negative`: The result exposed a user-relevant failure, risk, or sharp edge.
- `expected`: The result matched the current understanding and did not reveal new risk.
- `blocked`: The case did not produce a trustworthy observation.

Only fill `status` when there is a credible comparison basis. Otherwise use `observation_summary`, `result_flag`, and `confidence` to communicate what was learned without over-claiming certainty.

For comparison cases, use `observation_summary` and the final report to say whether the evidence supports pattern parity only or a broader quality claim.

If a case reveals a new branch of behavior, add a follow-up case instead of overloading the original one.

## Evidence Discipline

Treat a case as incomplete when:

- The observed output omits the key result you were testing.
- The script mixed multiple questions and the result is ambiguous.
- Hidden state, cache behavior, or previous runs may have influenced the result and were not controlled or documented.
- The question is whether behavior changed, but the case has no credible control or baseline to compare against.
- The case plans to read environment variables, but the exact variable names were not approved by the user before execution.
- The case was repeat-sensitive, but it ran only once without a clear rationale.

When this happens, narrow the probe and rerun. A smaller script with a cleaner result is better than a more complicated script that is hard to trust.
