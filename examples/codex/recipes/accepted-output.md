# Releasing an accepted answer

The application in [accepted_output.py](accepted_output.py) keeps draft text private while displaying its own progress messages. It releases an answer only after `await turn.get_result()` and application validation. This pattern serves report generation, support replies, and structured results using the existing native `Agent` and `Session` APIs.

## Running the example

Use an SDK build from the same checkout as this example. Follow the [source build instructions](../../../CONTRIBUTING.md#build-the-sdk-from-source), then activate the environment and run this command from the checkout root.

```sh
PYTHONPATH=. python -m examples.codex.recipes.accepted_output
```

`ScriptedResponses` supplies synthetic model responses through a loopback HTTP server and the normal native provider. The recipe runs without API credentials or model charges. The peer requires direct access or proxy bypass for `127.0.0.1` throughout execution. The section scenario uses one application function tool. Its in-memory lists and section map represent application publication.

The run checks these observable outcomes with assertions:

| Scenario | Released content |
| --- | --- |
| Report revision | One corrected report including the 2 pending checks |
| Approved support reply | One validated JSON answer for `synthetic-42` |
| Rejected approval | No answer |
| Cancellation during approval checking | No answer |
| Approval service failure | No answer |
| Wrong case identifier in completed JSON | No answer |
| Same-turn section publication | Inventory section before the next request; no delivery section or final summary |

The console displays application-owned progress, accepted content, and fixed failure statuses. The script withholds the first report draft with its unsupported success claim.

## Checking completion before publication

`check_report` inspects each proposed completion through `CompletionContext.text`. On the first incomplete report, it returns `ContinueTurn` with corrective feedback. The same turn requests a revised report within `RunBudget(max_requests=2)`. On another incomplete report, the callback returns `StopTurn`. `continuation_count` bounds the example's content retries as well.

The application retains `OutputTextDelta` observations privately, including the rejected draft. The SDK retains streamed observations and recorded history after completion rejection. `before_complete` does not retract them. The application therefore withholds model deltas, tool output, traces, and history from its customer-facing response. It manages access and retention separately for those stores.

The support example obtains approval from trusted application context. It checks the completed text with Pydantic and matches `case_id` against the application's expected case. Only then does it append to `published`. A successful completion precedes these application checks; it does not replace them. The existing [structured-output guide](../../../docs/sdk/outputs.md#return-a-typed-result) covers SDK output conversion.

## Handling interruption and failure

The rejected and cancelled turns raise `TurnStoppedError` through `get_result()`. An approval callback exception fails the turn. A wrong case identifier fails the application's post-completion validation. Each path skips publication and displays only an application-owned status. The cancellation example waits for approval checking before calling `await turn.cancel()`.

The recipe demonstrates private computation, accepted completion, application checks, then publication. The same sequence applies to a response body, an answer record, or an artifact download. Cancellation does not undo completed tool effects or earlier publication. The application's publishing operation manages durable writes, retries, concurrent updates, and delivery failures separately.

The synthetic report check matches a phrase to keep the example focused. Real report validation requires application-specific checks for facts, citations, and format. The scripted peer exercises application control flow and the native runtime; it does not evaluate live model quality.

## Publishing independent sections

The inventory and dispatch sections pass their own checks in separate turns. The example publishes each section before starting the next. The delivery section fails its check, so the example withholds it while keeping the earlier sections available.

This pattern applies when each section has its own evidence and permission to publish. When a later check can invalidate an earlier section, the whole answer requires a successful final result before delivery.

## Publishing sections within one turn

The same-turn scenario has been using an application-owned `publish_section` function tool. The application has been checking each proposed section against trusted facts before storing it by section ID in its in-memory destination. Repeating an accepted section has been replacing that section's entry. The peer has been verifying the inventory section's publication before responding to the next model request. It has then been proposing an unsupported delivery section, which the tool has been withholding. One submission has been covering both calls and a later final proposal.

The completion callback has been rejecting that final proposal. The application has been keeping the provisional summary private while retaining the independently accepted inventory section. Section publication has been taking effect inside the tool callback, before SDK acceptance of the tool result or final answer. Later tool policies, cancellation, and final-answer rejection have not been undoing that application effect. The application has been checking facts at its publication point; final-answer acceptance has been remaining separate.

This pattern has been supporting independently checked inventory facts and individually approved support updates. The example has been keeping delivery in memory. Production destinations have been requiring publication authorization, durable receipts, and reconciliation after an unknown acknowledgment. The in-memory section map has not been providing durable idempotency. Passive observations have been allowing gaps and have not been providing a durable delivery queue. Whole-answer checks that can invalidate earlier sections have been requiring publication after final acceptance instead. This recipe has not been turning raw text deltas into accepted incremental output.

## Observing accepted output

On the matching source build, `agent.session(include_output_accepted=True)` enables `OutputAccepted` from `agents.observation`. Turn and Session readers receive this metadata after a successful logical result, including any continuation and output validation. `submission_id` identifies the original submission. For a manually restored Turn, it identifies the restored handle in the current Session. `turn_id` identifies the final native step.

The event carries identity only. `await turn.get_result()` supplies the accepted result for the application's own checks and publication. Bounded readers can still report gaps, so the event serves as a notification rather than a delivery receipt.

## Approving text during generation

On the matching source build, `turn.accepted_text(accept)` has been exposing application-approved text before final settlement. The [accepted text example](accepted_text.py) has been using a synthetic stream and an exact prefix check without API credentials. The synthetic model has been waiting for the first approved prefix before finishing generation. The example has been requiring the [matching source build](../../../CONTRIBUTING.md#build-the-sdk-from-source).

The application callback has been receiving contiguous pending text for one root output item. Returning an exact string prefix has been approving only that prefix. Returning `None` or an empty string has been waiting for more text. The callback has been supporting synchronous and asynchronous checks. The reader has been removing each approved prefix and carrying its remaining text into the next callback. A new item has been discarding the prior unapproved tail. Each yielded `AcceptedTextChunk` has been carrying `session_id`, `submission_id`, native `turn_id`, `item_id`, and approved `text`.

The callback has been running only while its exact native root step remains live. An awaited callback has been losing publication authority after cancellation, stopping, settlement, or source replacement. Later reads of settled Turns have been producing no new approvals. Native generation has been continuing independently of the reader, and callback failures have been reaching only that reader. Closing the reader has been leaving generation unchanged.

The reader has been failing closed on observation gaps, invalid prefixes, and more than 65,536 UTF-8 bytes of pending text. Child text, linked private output repairs, and Realtime audio have been remaining outside this root text reader. Passive retention has been remaining bounded, so slower readers have been needing to handle a gap as a failed delivery attempt.

An approved chunk has been representing an irrevocable application decision for that text. A later provider retry or failed final result has not been undoing publication. Whole-answer checks that can invalidate earlier text have been requiring the existing final-result publication pattern. The identity-only `OutputAccepted` event has been following final acceptance separately. `accepted_text` has not been certifying final output, tool effects, delivery, or billing.
