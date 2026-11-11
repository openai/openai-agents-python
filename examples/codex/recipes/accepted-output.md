# Releasing an accepted answer

The application in [accepted_output.py](accepted_output.py) has been keeping draft text private while displaying its own progress messages. It has been releasing an answer only after `await turn.get_result()` and application validation. This pattern has been serving report generation, support replies, and structured results using the existing native `Agent` and `Session` APIs.

## Running the example

Use an SDK build from the same checkout as this example. Follow the [source build instructions](../../../CONTRIBUTING.md#build-the-sdk-from-source), then activate the environment and run this command from the checkout root.

```sh
PYTHONPATH=. python -m examples.codex.recipes.accepted_output
```

`ScriptedResponses` has been supplying synthetic model responses through a loopback HTTP server and the normal native provider. The recipe has been running without API credentials or model charges. The peer has been requiring direct access or proxy bypass for `127.0.0.1` throughout execution. The script has been using no tools or external writes. Its `published` lists have been representing application publication in memory.

The run has been checking these observable outcomes with assertions:

| Scenario | Released content |
| --- | --- |
| Report revision | One corrected report including the 2 pending checks |
| Approved support reply | One validated JSON answer for `synthetic-42` |
| Rejected approval | No answer |
| Cancellation during approval checking | No answer |
| Approval service failure | No answer |
| Wrong case identifier in completed JSON | No answer |

The console has been displaying application-owned progress, accepted content, and fixed failure statuses. The script has been withholding the first report draft with its unsupported success claim.

## Checking completion before publication

`check_report` has been inspecting each proposed completion through `CompletionContext.text`. On the first incomplete report, it has been returning `ContinueTurn` with corrective feedback. The same turn has been requesting a revised report within `RunBudget(max_requests=2)`. On another incomplete report, the callback has been returning `StopTurn`. `continuation_count` has been bounding the example's content retries as well.

The application has been retaining `OutputTextDelta` observations privately, including the rejected draft. The SDK has been retaining streamed observations and recorded history after completion rejection. `before_complete` has not been retracting them. The application has therefore been withholding model deltas, tool output, traces, and history from its customer-facing response. It has been managing access and retention separately for those stores.

The support example has been obtaining approval from trusted application context. It has been checking the completed text with Pydantic and matching `case_id` against the application's expected case. Only then has it been appending to `published`. A successful completion has been preceding these application checks; it has not been replacing them. The existing [structured-output guide](../../../docs/sdk/configuration.md#structured-output) has been covering SDK output conversion.

## Handling interruption and failure

The rejected and cancelled turns have been raising `TurnStoppedError` through `get_result()`. An approval callback exception has been failing the turn. A wrong case identifier has been failing the application's post-completion validation. Each path has been skipping the publication statement and displaying only an application-owned status. The cancellation example has been waiting for approval checking before calling `await turn.cancel()`.

The recipe has been demonstrating private computation, accepted completion, application checks, then publication. The same sequence has been applying to a response body, an answer record, or an artifact download. Cancellation has not been undoing completed tool effects or earlier publication. The application's publishing operation has been managing durable writes, retries, concurrent updates, and delivery failures separately.

The synthetic report check has been matching a phrase to keep the example focused. Real report validation has been requiring application-specific checks for facts, citations, and format. The scripted peer has been exercising application control flow and the native runtime; it has not been evaluating live model quality.
