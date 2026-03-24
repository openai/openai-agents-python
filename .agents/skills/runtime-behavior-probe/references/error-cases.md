# Common Error Cases

Use this reference to expand beyond the happy path. Favor error cases that a real user or operator is likely to hit.

## Configuration Errors

Check whether the runtime behaves differently for:

- Missing required environment variables.
- Present but malformed secrets or identifiers.
- Wrong endpoint or base URL.
- Wrong model or deployment name.
- Incompatible local dependency versions.

Look for:

- Error type and status code.
- Whether the failure is immediate or delayed.
- Whether the message is actionable.
- Whether retrying without fixing configuration changes anything.

## Input Errors

Probe common bad-input patterns such as:

- Missing required fields.
- Wrong data type.
- Unsupported enum or option value.
- Empty but syntactically valid input.
- Oversized input or too many items.
- Mutually incompatible options.

Prefer realistic invalid inputs over artificial nonsense. The point is to learn how the runtime fails in practice.

## Transport and Availability Errors

When networked services are involved, consider:

- Connection failure.
- Read timeout.
- Server timeout or upstream gateway error.
- Rate limit response.
- Partial stream interruption.
- Reusing a connection after a failure.

Capture whether the client library retries automatically, whether it surfaces retry metadata, and whether the final exception preserves the original cause.

## State and Repetition Errors

Many surprising bugs appear only when an operation is repeated or interrupted:

- Re-submit the same request.
- Repeat after a timeout.
- Retry after a partial tool call or partial stream.
- Resume after local cleanup or process restart.
- Repeat with slightly changed inputs while reusing shared state.

Observe whether the operation is idempotent, duplicated, silently ignored, or left in a partial state.

## Concurrency Errors

When shared state, ordering, or isolation may matter, consider:

- Two overlapping requests with the same logical input.
- Parallel runs that reuse the same cache key, session, container, or temporary resource.
- Concurrent retries, cancellation, or cleanup racing with active work.
- Output or event streams from one run leaking into another.

Capture whether the runtime serializes, rejects, duplicates, corrupts, or cross-contaminates the work.

## Investigation Heuristics

Use these heuristics to pick error cases quickly:

- Ask which failure a real engineer would debug first in production.
- Ask which failure is most expensive if it is misunderstood.
- Ask which failure would be invisible from code review alone.
- Ask which failure path is likely to differ across environments.

If the error behavior is already perfectly obvious from a local validator or type system, it is usually low priority for this skill.
