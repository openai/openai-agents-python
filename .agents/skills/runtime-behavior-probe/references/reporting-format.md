# Reporting Format

Lead with findings, not process. The user asked for investigation results, so the answer should start with the most important observed behaviors. Put the real news first.

## Recommended Order

1. Findings.
2. Validation approach.
3. Case matrix or condensed case summary.
4. Artifact status and brief run summary.
5. Optional implementation note.

## Findings Section

Make each finding answer one user-relevant question. Good findings usually include:

- What was observed.
- Why it matters.
- The condition under which it happens.
- What was held constant when the finding comes from a comparison probe.
- `scope`: The boundary of the finding, such as commit, model, Python version, live vs local, or repeat mode.
- `confidence`: `high`, `medium`, or `low`.

Avoid burying the main result under setup details.

Put `unexpected` or `negative` findings first. If there were no unexpected or negative findings in the executed cases, say that explicitly before the rest of the findings section.

If the probe was comparative, say whether the result supports:

- Pattern parity only.
- A broader quality claim.

Do not imply a broader quality equivalence than the executed cases justify.

## Validation Approach Section

Summarize:

- The runtime surface you exercised.
- The shape of the probe code, in overview only.
- Which categories of cases you covered.
- Which execution modes you used, including repeat counts or warm-up handling when relevant.
- Whether live credentials or external services were used.
- Any important state controls such as fresh state, cache reuse, cache busting, unique IDs, or cleanup verification.
- For comparison probes, what was held constant, what was varied, and whether output-shape or usage differences could still influence the conclusion.
- Whether the usual docs path or an official-docs fallback was used for contract-sensitive checks.

Keep this concise. The user needs enough detail to trust the result, not a line-by-line replay of the script.

## Case Summary

Include either the full matrix or a condensed summary. At minimum, show:

- Which scenarios were executed.
- Whether the run was a quick pilot, an expanded matrix, or both.
- Which ones produced `unexpected` or `negative` results.
- Which ones passed or failed when a real comparison basis existed.
- Which cases were blocked.
- Where the supporting evidence lived, or that it was deleted.

If the matrix is large, show the highest-value cases in the main response and keep the rest as a compact appendix or note.

## Artifact Status And Brief Run Summary

State one of these explicitly:

- Temporary artifacts were kept until the final response was drafted, then deleted after validation.
- Temporary artifacts were kept at `<path>` because the user asked to keep them.
- Temporary artifacts were kept at `<path>` because they are needed for follow-up analysis.

Even if artifacts were deleted, retain a short run summary such as:

- Probe command or runner shape.
- Runtime context summary such as commit, Python executable, Python version, or model.
- Artifact path and final status.

For benchmark or repeat-heavy probes, keeping artifacts for follow-up is often the right default even when the immediate report is done.

## Optional Implementation Note

Include this only when one clear defect was isolated and a short implementation hypothesis or minimal repro direction would help. Keep it brief. Do not turn the report into a broader next-step plan unless the user asked for that.

## Compact Template

Use this outline when you need a fast structure:

    Findings:
    - <finding 1>
      held constant: <prompt/tool/state settings kept the same, if comparative>
      scope: <commit/model/python/live-local/repeat-mode>
      confidence: <high|medium|low>
    - <finding 2>
      held constant: <prompt/tool/state settings kept the same, if comparative>
      scope: <commit/model/python/live-local/repeat-mode>
      confidence: <high|medium|low>

    Validation approach:
    - Surface: <what was exercised>
    - Probe code: <brief overview>
    - Coverage: <success, edge, error, repeat-sensitive, and quality categories>
    - Execution modes: <single-shot|repeat-N|warm-up + repeat-N>
    - Comparison parity: <what was held constant and what varied, if comparative>
    - Docs source: <MCP or official-docs fallback, if relevant>

    Case summary:
    | case_id | scenario | result_flag | status | note |
    | --- | --- | --- | --- | --- |
    | S1 | ... | expected | pass | ... |
    | E1 | ... | negative | fail | ... |

    Artifact status and brief run summary:
    - Temporary artifacts were kept until the final response was drafted, then deleted.
    - Summary: <command/runtime-context/artifact-status summary>

    Optional implementation note:
    - <brief hypothesis or minimal repro direction>

Adjust the format to the task, but preserve the ordering.
