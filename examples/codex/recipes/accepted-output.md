# Releasing an accepted answer

The application in [accepted_output.py](accepted_output.py) has been keeping draft text private while displaying its own progress messages. It has been releasing an answer only after `await turn.get_result()` and application validation. This pattern has been serving report generation, support replies, and structured results using the existing native `Agent` and `Session` APIs.

## Running the example

Use an SDK build from the same checkout as this example. Follow the [source build instructions](../../../CONTRIBUTING.md#build-the-sdk-from-source), then activate the environment and run this command from the checkout root.

```sh
PYTHONPATH=. python -m examples.codex.recipes.accepted_output
```

`ScriptedResponses` has been supplying synthetic model responses through a loopback HTTP server and the normal native provider. The recipe has been running without API credentials or model charges. The peer has been requiring direct access or proxy bypass for `127.0.0.1` throughout execution. The section scenario has been using one application function tool. Its in-memory lists and section map have been representing application publication.

The run has been checking these observable outcomes with assertions:

| Scenario | Released content |
| --- | --- |
| Report revision | One corrected report including the 2 pending checks |
| Approved support reply | One validated JSON answer for `synthetic-42` |
| Rejected approval | No answer |
| Cancellation during approval checking | No answer |
| Approval service failure | No answer |
| Wrong case identifier in completed JSON | No answer |
| Same-turn section publication | Inventory section before the next request; no delivery section or final summary |

The console has been displaying application-owned progress, accepted content, and fixed failure statuses. The script has been withholding the first report draft with its unsupported success claim.

## Checking completion before publication

`check_report` has been inspecting each proposed completion through `CompletionContext.text`. On the first incomplete report, it has been returning `ContinueTurn` with corrective feedback. The same turn has been requesting a revised report within `RunBudget(max_requests=2)`. On another incomplete report, the callback has been returning `StopTurn`. `continuation_count` has been bounding the example's content retries as well.

The application has been retaining `OutputTextDelta` observations privately, including the rejected draft. The SDK has been retaining streamed observations and recorded history after completion rejection. `before_complete` has not been retracting them. The application has therefore been withholding model deltas, tool output, traces, and history from its customer-facing response. It has been managing access and retention separately for those stores.

The support example has been obtaining approval from trusted application context. It has been checking the completed text with Pydantic and matching `case_id` against the application's expected case. Only then has it been appending to `published`. A successful completion has been preceding these application checks; it has not been replacing them. The existing [structured-output guide](../../../docs/sdk/configuration.md#structured-output) has been covering SDK output conversion.

## Handling interruption and failure

The rejected and cancelled turns have been raising `TurnStoppedError` through `get_result()`. An approval callback exception has been failing the turn. A wrong case identifier has been failing the application's post-completion validation. Each path has been skipping the publication statement and displaying only an application-owned status. The cancellation example has been waiting for approval checking before calling `await turn.cancel()`.

The recipe has been demonstrating private computation, accepted completion, application checks, then publication. The same sequence has been applying to a response body, an answer record, or an artifact download. Cancellation has not been undoing completed tool effects or earlier publication. The application's publishing operation has been managing durable writes, retries, concurrent updates, and delivery failures separately.

The synthetic report check has been matching a phrase to keep the example focused. Real report validation has been requiring application-specific checks for facts, citations, and format. The scripted peer has been exercising application control flow and the native runtime; it has not been evaluating live model quality.

## Publishing independent sections

The inventory and dispatch sections have been passing their own checks in separate turns. The example has been publishing each section before starting the next. The delivery section has been failing its check, so the example has been withholding it while keeping the earlier sections available.

This pattern has been applying when each section has its own evidence and permission to publish. When a later check can invalidate an earlier section, the whole answer has been requiring a successful final result before delivery.

## Publishing sections within one turn

The same-turn scenario has been using an application-owned `publish_section` function tool. The application has been checking each proposed section against trusted facts before storing it by section ID in its in-memory destination. Repeating an accepted section has been replacing that section's entry. The peer has been verifying the inventory section's publication before responding to the next model request. It has then been proposing an unsupported delivery section, which the tool has been withholding. One submission has been covering both calls and a later final proposal.

The completion callback has been rejecting that final proposal. The application has been keeping the provisional summary private while retaining the independently accepted inventory section. Section publication has been taking effect inside the tool callback, before SDK acceptance of the tool result or final answer. Later tool policies, cancellation, and final-answer rejection have not been undoing that application effect. The application has been checking facts at its publication point; final-answer acceptance has been remaining separate.

This pattern has been supporting independently checked inventory facts and individually approved support updates. The example has been keeping delivery in memory. Production destinations have been requiring publication authorization, durable receipts, and reconciliation after an unknown acknowledgment. The in-memory section map has not been providing durable idempotency. Passive observations have been allowing gaps and have not been providing a durable delivery queue. Whole-answer checks that can invalidate earlier sections have been requiring publication after final acceptance instead. This recipe has not been turning raw text deltas into accepted incremental output.

## Observing accepted output

On the matching source build, `agent.session(include_output_accepted=True)` has been enabling `OutputAccepted` from `agents.observation`. Turn and Session readers have been receiving this metadata after a successful logical result, including any continuation and output validation. `submission_id` has been identifying the original submission. For a manually restored Turn, it has been identifying the restored handle in the current Session. `turn_id` has been identifying the final native step.

The event has been carrying identity only. `await turn.get_result()` has been supplying the accepted result for the application's own checks and publication. Bounded readers have still been able to report gaps, so the event has been serving as a notification rather than a delivery receipt.

## Approving text during generation

On the matching source build, `turn.accepted_text(accept)` has been exposing application-approved text before final settlement. The [accepted text example](accepted_text.py) has been using a synthetic stream and an exact prefix check without API credentials. The synthetic model has been waiting for the first approved prefix before finishing generation. The example has been requiring the [matching source build](../../../CONTRIBUTING.md#build-the-sdk-from-source).

The application callback has been receiving contiguous pending text for one root output item. Returning an exact string prefix has been approving only that prefix. Returning `None` or an empty string has been waiting for more text. The callback has been supporting synchronous and asynchronous checks. The reader has been removing each approved prefix and carrying its remaining text into the next callback. A new item has been discarding the prior unapproved tail. Each yielded `AcceptedTextChunk` has been carrying `session_id`, `submission_id`, native `turn_id`, `item_id`, and approved `text`.

The callback has been running only while its exact native root step remains live. An awaited callback has been losing publication authority after cancellation, stopping, settlement, or source replacement. Later reads of settled Turns have been producing no new approvals. Native generation has been continuing independently of the reader, and callback failures have been reaching only that reader. Closing the reader has been leaving generation unchanged.

The reader has been failing closed on observation gaps, invalid prefixes, and more than 65,536 UTF-8 bytes of pending text. Child text, linked private output repairs, and Realtime audio have been remaining outside this root text reader. Passive retention has been remaining bounded, so slower readers have been needing to handle a gap as a failed delivery attempt.

An approved chunk has been representing an irrevocable application decision for that text. A later provider retry or failed final result has not been undoing publication. Whole-answer checks that can invalidate earlier text have been requiring the existing final-result publication pattern. The identity-only `OutputAccepted` event has been following final acceptance separately. `accepted_text` has not been certifying final output, tool effects, delivery, or billing.
