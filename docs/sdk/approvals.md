# Approvals and manual tool results

Use a manual `FunctionTool` when your application must obtain or supply a tool result. Handle supported native approvals and MCP forms through the same interaction API. Your application owns the decision or result. The live request identifies the call that may receive the reply.

| Your application needs to | Use |
| --- | --- |
| Supply a typed function result | [A manual tool](#manual-tool-results) |
| Wait for requests during an explicit Session | [Wait for a request or result](#wait-for-a-request-or-result) |
| Handle recurring requests across Session turns | [A retained Session handler](#handle-requests-for-a-retained-session) |
| Answer requests during `run()` | [A one-shot handler](#answer-interactions-during-a-one-shot-run) |
| Answer a native approval, form, or URL request | [Native interaction requests](#handle-native-approvals-and-forms) |
| Answer a trusted service's verification challenge | [Verification proofs](#answer-a-verification-challenge) |
| Expire a live request | [Force cancellation](#expire-pending-requests) |
| Recover an external job after a host restart | [Experimental job recovery](#experimental-job-recovery) |

[Install the matching SDK](installation.md) and [configure the provider](configuration.md#provider-connection) before running these examples. Snippets with top-level `await` belong inside an async application. For conversation checkpoints and suspension eligibility, use [state and recovery](state.md).

## Manual tool results

Use `FunctionTool` when the application must supply a tool result later. The declaration has no function body. Its `input_type` is a Pydantic model. The SDK validates the result against `output_type` before sending it to the native runtime.

```python
from agents import Agent, FunctionTool
from pydantic import BaseModel


class LookupInput(BaseModel):
    key: str


class LookupOutput(BaseModel):
    label: str


lookup = FunctionTool(
    name="lookup",
    description="Look up a label by key.",
    input_type=LookupInput,
    output_type=LookupOutput,
)
agent = Agent(instructions="Use lookup to find labels.", tools=[lookup])


async def answer(event):
    request = lookup.match(event)
    if request is None:
        raise ValueError("Unexpected manual tool")
    value = LookupOutput(label=f"Label for {request.arguments.key}")
    return request.reply(value)


result = await agent.run("Find the label for record-a.", on_interaction=answer)
```

### Continue a one-shot manual request

For a run using Python or manual tools with plain-text or native strict Agent output, `agent.run()` and `agent.run_sync()` can start without `on_interaction`. If the model never calls a manual tool, the run completes normally. If it calls one, the SDK waits for a supported settled manual boundary, then raises `InteractionRequired` with a portable checkpoint. Completed tool effects are not replayed on restore.

```python
from agents import InteractionRequired

try:
    completed = await agent.run("Find the label for record-a.")
except InteractionRequired as exc:
    if exc.state is None:
        raise
    completed = await agent.run(None, state=exc.state, on_interaction=answer)
```

Run this fragment with the `agent` and `answer` definitions from [Manual tool results](#manual-tool-results). Always check `exc.state`.

For automatic-checkpoint eligibility and the rules for resuming pending work, see [manual-request checkpoints](state.md#automatic-checkpoint-limits).

### Reply to live manual requests

`match()` accepts only requests issued for that exact declaration object. It also preserves the request's input and output types for static checking. Manual calls can wait concurrently. Reply to each request separately. Answering one request does not consume another.

A live manual request is limited to 65,536 bytes of UTF-8 encoded request data, including its arguments and request metadata. An oversized request fails its owning submission with `NativeError` before argument validation or delivery to a handler. Keep manual-tool payloads within this limit.

Choose the reply method for the way you run the agent:

| How you run the agent | How to reply |
| --- | --- |
| `agent.run(..., on_interaction=handler)` | Return `request.reply(value)` from the handler. |
| An explicit Session | Read `session.interactions()` and call `await request.respond(value)`. |

If validation fails in a live Session, you can retry the request. Duplicate replies, replies to expired requests, and replies for another waiting call fail. Native acceptance delivers the reply to the original call. Acceptance does not confirm a durable history write or start a new user turn.

`session.interactions()` also delivers child requests after the root finishes. `request.subagent_id` identifies the child, or is `None` for root work. Requests from different children have separate authority even if call IDs coincide. Manual tools work in direct mode and programmatic tool calling. Pending child requests and pending code cells require a live Session.

`ActionRequested` contains sensitive typed arguments and lets the application reply to a live request. Avoid logging or storing it. Passive events do not let the application reply. A new interaction cursor receives still-pending requests without replay sequence numbers.

<a id="reply-to-live-requests"></a>

### Wait for a request or result

To wait for one Turn's requests or completion, use `await turn.wait_for_request_or_result()`.

The method returns one of two public types from `agents`:

- `Completed` contains the result.
- `PendingRequests` contains the captured `turn` and a tuple of live `requests`. The snapshot reserves nothing, so another consumer can answer first.

After answering a batch, wait again for later requests or completion.

This independent async example declares a manual tool with a string result, matches each request to that declaration, and supplies the result:

```python
from pydantic import BaseModel

from agents import (
    Agent,
    Completed,
    FunctionTool,
    InteractionExpiredError,
    PendingRequests,
)


class LookupArgs(BaseModel):
    key: str


lookup = FunctionTool(
    name="lookup",
    description="Read a saved label.",
    input_type=LookupArgs,
    output_type=str,
)
agent = Agent(instructions="Use lookup to read the label.", tools=[lookup])
async with agent.session() as session:
    turn = await session.submit("Look up atlas.")
    while True:
        outcome = await turn.wait_for_request_or_result()
        if isinstance(outcome, Completed):
            print(outcome.final_output)
            break
        assert isinstance(outcome, PendingRequests)
        for request in outcome.requests:
            manual = lookup.match(request)
            if manual is None:
                raise TypeError("Unexpected interaction for this tool selection")
            try:
                await manual.respond(f"Saved label: {manual.arguments.key}")
            except InteractionExpiredError:
                continue
```

Choose the request scope you need:

- On a root Turn, `wait_for_request_or_result()` includes its continuations but excludes child requests.
- `session.interactions()` and `session.pending_interactions()` cover the Session and its children.

Match manual requests with `FunctionTool.match()`. Other native request types need their own reply handling. Closing the Session prevents outstanding replies. Cancelling a waiter leaves the Turn running.

### Expire pending requests

To expire pending root requests, use `await turn.cancel(mode="force")`. To include children and the queue, use `await session.cancel_all(mode="force")`. Session exit also expires pending requests. Cancelling a `turn.get_result()` task or closing an observation cursor leaves them live. See [interrupt work](sessions.md#interrupt-work).

### Handle requests for a retained Session

An optional Session handler uses the same bound replies as a one-shot handler. This fragment reuses `agent` and `answer` from [Manual tool results](#manual-tool-results):

```python
async with agent.session(on_interaction=answer) as session:
    turn = await session.submit("Find the label for record-a.")
    result = await turn.get_result()
```

The handler remains registered across root turns. It receives supported requests from the Session registry, including URL requests with no Turn IDs. The handler does not enable unsupported startup prompts. Handler failures reach their identified Turn; a failure for a Session-scoped request fails the Session. Closing the Session joins started handlers. A root result alone does not close a retained Session.

Applications that own their interaction tasks can keep using `session.interactions()`. Deliver the same prepared reply with `await session.respond(await answer(request))`. A reply does not reserve its request; another consumer can answer first, and delivery then raises `InteractionExpiredError`. Preserve the exact-request check when building reusable handlers.

### Answer interactions during a one-shot run

Pass `on_interaction` to `agent.run()` or `agent.run_sync()`. The handler returns the reply created by the exact request it receives, directly or through an awaitable. For a manual function, use the [`answer` handler](#manual-tool-results). The SDK validates and submits the reply. Do not call `respond()` in a handler and return `None`. That fails, as does a reply for a different, expired or already answered request.

#### Handle native approvals and forms

The handler can receive supported native root requests too. Approval requests use `request.reply("allow_once")` or `request.reply("deny")`. Form requests use `request.reply(values)`; `request.reply(None)` declines a form or URL request. Native acknowledgement means the reply was enqueued, not that an external approval or URL verification succeeded. Native child requests require `NativeAgents(interactions=True)`; manual child requests do not require that option.

Import `ApprovalRequest`, `FormRequest` and `UrlRequest` from `agents` to distinguish native approval, form and URL requests. `ElicitationRequest` is the union of the form and URL variants. Their `mode` fields are `"form"` and `"url"`, respectively:

| Request | Inspect | One-shot reply | Explicit Session reply |
| --- | --- | --- | --- |
| `ApprovalRequest` | `description` | `reply("allow_once")` or `reply("deny")` | `await respond("allow_once")` or `await respond("deny")` |
| `FormRequest` | `description` and `schema` | `reply(values)` or `reply(None)` to decline | `await respond(values)` or `await decline()` |
| `UrlRequest` | `description`, `url` and `elicitation_id` | `reply_url_accept()` to consent or `reply(None)` to decline | `await accept_url()` to consent or `await decline()` |

This handler requires an asynchronous application function `collect_form_values(description, schema)`. That function must render the supplied fields and return the user's answer mapping, or `None` to decline. Pass `answer_native` as `on_interaction` when the run expects native requests:

```python
from agents import ApprovalRequest, FormRequest, UrlRequest


async def answer_native(request):
    if isinstance(request, ApprovalRequest):
        return request.reply("deny")
    if isinstance(request, UrlRequest):
        return request.reply(None)
    if isinstance(request, FormRequest):
        values = await collect_form_values(request.description, request.schema)
        return request.reply(values)
    raise TypeError("Unexpected interaction")
```

#### Form schemas and handler lifetime

`FormRequest.reply(values)` validates the full supported schema immediately and captures detached JSON. A handler can catch `ValueError` around that call and request a corrected answer before returning. Delivery still verifies the live request. Manual tool output validation happens at delivery and has a separate contract.

`FormRequest.schema` is a detached schema mapping. The SDK validates form replies against the supported supplied field constraints. The SDK never visits a URL. A Session-scoped URL request has `turn_id=None` and `submission_id=None`, even while a turn is active. Do not infer an originating turn from the Session's current work.

URL consent does not establish completion or verification. In an explicit Session, after consent, `await request.wait_url_completion(timeout=30)` waits for the issuing server to report completion for that URL interaction. The application decides whether to trust that server's report. A timeout raises `TimeoutError` and stops only that observer. Cancelling the waiter also stops only that wait. To expire the shared lifecycle, call `await request.cancel_url_completion()` explicitly; other completion waiters then receive `InteractionExpiredError`. Keep the Session open while waiting. The preceding handler declines URLs by default.

Handlers may run concurrently. Synchronous handlers run in worker threads and must support concurrent calls. When root work finishes, fails, or is cancelled, the run closes its Session and waits for handlers to finish. Python cannot forcibly stop synchronous worker threads. Use an explicit Session if children must continue after the root finishes or if you need to retry an invalid manual result.

Standard MCP forms and OpenAI extension forms support these schema modes:

| Schema mode | Supported values and validation |
| --- | --- |
| Strict primitive schemas in standard forms | Primitive fields and enum-backed string multi-select arrays, including titled choices. The SDK checks types, bounds, choices and supported formats. |
| General schemas in standard or OpenAI extension forms | Draft 2020-12 object schemas with nested objects, arrays, root constraints and single-hop references to local `$defs`. The SDK validates JSON values; `format` is an annotation. |

A standard form that does not fit the strict primitive subset uses general validation. For example, its schema can require a nested `profile.active` boolean and a `scores` array of integers. General schemas do not support regex keywords, resource identifiers, dynamic references or references beyond single-hop local `$defs`. The SDK does not fetch schema resources.

Native child interactions require the [explicit opt-in](delegation.md#shared-tools-and-permissions). Use manual tools for application-owned questions, including supported child manual requests. Live replies do not make code cells, processes or child turns portable.

### Answer a verification challenge

`VerificationRequest` is separate from a form or URL consent request. It is available only with an explicit [trusted verification or personal Apps registration](mcp.md#service-specific-integrations). These integrations require separately provisioned service access and credentials. SDK installation does not provide enrollment, signing keys or access to the service.

For a verification request, `challenge` contains the exact bytes to sign. `server_name` identifies the host registration; the service-provided `title` and `description` grant no authority. Use only the signer enrolled for that registration. Never persist the live request, challenge or proof.

The following handler fragment requires your application's asynchronous `sign_challenge(challenge: bytes) -> VerificationProof` function. That function must sign the exact bytes with its enrolled key and return `VerificationProof(credential_id=..., signature=...)`. Encode the signature as canonical URL-safe base64 without `=` padding. It must decode to 1–128 bytes. The nonempty credential ID must be at most 1,024 UTF-8 bytes.

This fragment reuses `answer_native` above for other native requests. Pass `answer_verification` as `on_interaction` for the Agent configured with your registration:

```python
from agents import VerificationProof, VerificationRequest


async def answer_verification(request):
    if isinstance(request, VerificationRequest):
        proof: VerificationProof = await sign_challenge(request.challenge)
        return request.reply(proof)
    return await answer_native(request)
```

Return `request.reply(None)` to decline instead of signing. In an explicit Session, use `await request.respond(proof)` or `await request.decline()`. A reply acknowledges enqueueing, not acceptance by the authoritative verification service. The service must verify and consume the challenge before reporting success. URL consent or completed connector OAuth does not automatically sign a later challenge.

### Ask for application approval

Choose the policy that owns the operation:

| Operation | Application control |
| --- | --- |
| Executor call | [`Environment.approval`](execution.md#executor-policies) |
| MCP call | [`before`, `approval`, and `transform_result`](mcp.md#mcp-result-policies), alongside native `approval_mode` |
| Native collaboration call | [`NativeAgents.approval`](delegation.md#native-tool-decisions) |

MCP declarations do not accept a per-call `interceptor`.

For questions your application asks, declare a `FunctionTool` with typed question arguments and a typed answer. Its `on_interaction` handler can wait for the user and return a reply created by that exact request. This also works in native children and programmatic tool calling while the run remains active.

## Experimental job recovery

`DeferredTools` connects a manual `FunctionTool` to an application-owned external job so the application can resume after a host restart. Your host manages the durable journal, current permissions, provider configuration, and wake-up schedule. Import the helper from `agents.experimental.deferred`. Recovery applies only to registered manual tools that meet the limits below; arbitrary tool calls do not become restartable.

### Declare and register a recoverable tool

Declare the tool, then register the exact object used by the Session. Registration returns `None` and rejects decorated callbacks.

Recovery declarations must meet these requirements:

- Input and result types are strict Pydantic models with `extra="forbid"`.
- Supported values are JSON strings, booleans, integers, finite floats, nulls, ordered lists, and nested strict models.
- Inputs can use JSON scalar defaults, `Literal`, and supported unions. Aliases must identify provider fields unambiguously.
- Results require every public field and preserve field and list order. Nullable fields are supported, but defaults, `Literal`, and general unions are not.
- Generated request IDs belong in the original input, never in default factories. Restore must not create new IDs.

Registration rejects custom validators, serializers, default factories, computed fields, custom initialization, and rich types such as `datetime` or `set`. Check custom inputs in host or provider code before external effects, and convert richer results to supported public models. Use live manual replies for custom serialization or scalar results.

```python
from pydantic import BaseModel, ConfigDict

from agents import FunctionTool
from agents.experimental.deferred import DeferredTools


class ReportInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    report_key: str


class ReportResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    row_count: int


report = FunctionTool(
    name="report",
    description="Count the rows in a report.",
    input_type=ReportInput,
    output_type=ReportResult,
)
```

Configure an Agent with `tools=[report]`. The following fragment requires an application implementation of `JobJournal` and a `JobProvider[ReportInput]` that returns a row count:

```python
jobs = DeferredTools(journal=journal, tenant=tenant, run_id=run_id)
jobs.register(
    report,
    provider_id="report.v1",
    provider=provider,
    project=lambda value: ReportResult.model_validate({"row_count": value}),
)
```

The host's `project` function selects fields the model may receive. `max_output_bytes` limits public result JSON to 16,384 bytes by default. Keep provider receipts and private values in the host journal.

### Prepare, restore, and advance

Run recovery from host code outside `on_interaction`:

1. Match a live root request with `report.match(request)`.
2. Call `await jobs.prepare(session, request)`. Preparation suspends the Session and expires live requests without calling the provider. Before preparation returns, the journal must atomically save the checkpoint and all registered pending bindings. If the save fails or is interrupted, inspect the journal before continuing.
3. Restore the prepared checkpoint with a matching Agent and obtain a fresh request from `restored.interactions()`.
4. Call `await jobs.advance(restored, request)`. If it returns a `Turn`, await `get_result()` for resumed model work. Strings such as `"pending"`, `"input_required"`, and `"unknown"` are host-only statuses.

Keep the original prepared checkpoint while sibling calls remain outstanding. Later restores can deliver public results retained in the journal without restarting jobs.

`advance()` has no background poller:

- `wait_seconds=0.0` (default) starts once without polling. The host schedules later observations.
- A positive wait permits one additional bounded observation after creation.
- `timeout_seconds=10` (default) covers authorization, provider I/O, persistence, and delivery. Cleanup is cooperative. A timeout does not authorize another creation.

For a single call, `async with jobs.start(session, request) as (restored, outcome)` prepares, restores, and advances while managing the restored Session. It uses declarations and resolved model credentials captured at opening and rejects unsupported changes before suspension.

If credentials, definitions, or instructions changed, prepare and restore explicitly with current configuration.

### Recovery limits and duplicate prevention

Prepare a root request before its first restore. `DeferredTools` supports plain-text and supported `PromptedOutput` roots. This helper rejects pending native-schema output, requests using an Environment, native agents, programmatic tool calling, and child requests. Use live replies for configurations the helper rejects. A settled root request with native strict output can also use [explicit Session suspension](state.md#save-a-settled-manual-request), with a matching output declaration on restore.

The journal must let only one worker claim creation and must check current permissions, exact binding, and generation on every operation.

By default, registration requires `provider.reconcile_creation()` and target-side duplicate prevention for an immutable job key. If a creation acknowledgment is lost, reconciliation must establish whether the job started before the host proceeds. The acknowledgment identifies the job, not its final result.

Set `require_reconciliation=False` only for approved, low-impact work that can remain `"unknown"`. An unknown outcome gives no permission to repeat an external effect.

Automatic delivery rejects public results that contain the current model API key, job key, or provider receipt, even as an incidental substring. Do not start the job again after this rejection. The host can review the result and use `await request.respond(safe_value)` on the fresh live request. The host must record that completion itself.

### Optional MCP Tasks adapter

Use this adapter when your application owns an external job and its durable journal. To let Codex call an MCP server during a live conversation, use the [native MCP guide](mcp.md).

`MCPTasksProvider` is an optional experimental Python adapter for one host-selected remote MCP tool. The adapter requires exactly `mcp==2.0.0` and `mcp-types==2.0.0`, then imports through `agents.experimental.mcp_tasks`. Normal native SDK imports do not require these packages.

The SDK package retains Classic's `mcp>=1.30.0, <3, !=2.0.*, !=2.1.*` dependency constraint. That constraint conflicts with the Tasks adapter's exact `2.0.0` requirement, so Classic and MCP Tasks cannot share one supported dependency environment. Isolate any Tasks experiments from Classic workers in separate environments. A second environment alone does not resolve installation of the SDK distribution with MCP 2.0.0: the distribution itself retains Classic's constraint. Do not force installation or override that constraint. The experimental adapter has no supported standalone installation recipe here.

#### Configure the remote tool

Construct `MCPTasksProvider(endpoint=..., tool=..., authorize=..., admit_pending=..., admit_result=...)` with trusted host configuration. Tool arguments cannot select the endpoint or tool. `authorize(claim, operation)` checks current permissions and returns a bearer token without its prefix for each HTTP request. The adapter refuses redirects. HTTPS uses the host's proxy and CA settings. Construct a fresh provider after changing the host's outbound network settings.

This adapter cannot reconcile lost creation receipts. Register it with `require_reconciliation=False` only for approved, low-impact work that can remain `"unknown"`. Before creation, the adapter reads at most 32 pages of tool declarations. If page 32 still has a continuation cursor, the adapter refuses the listing before sending `tools/call`.

#### Admit pending requests and results

Supply a fast synchronous `admit_pending(claim, task_id, requests_json)` callback. At creation, `requests_json` is `None`. For a form, it holds the complete request mapping as compact JSON with sorted keys.

Return exactly `True` only when trusted data for `claim.binding` confirms the task ID and every form key, prompt, and schema. Treat these values as private even on refusal; a peer can copy earlier credentials into them.

Missing or refused admission leaves new creation `"unknown"` without a receipt. A refused form remains `"unknown"` with its accepted receipt. Neither permits another creation.

Completed updates omit peer text, structured fields, and metadata by default. To admit data, supply a fast synchronous `admit_result(claim, result)` callback returning JSON with finite numbers. The adapter stores that data under `structuredContent` for the projection function and retains the peer's `isError` flag.

Check fields against trusted per-job data when independent verification matters. Restore the admission policy with the job. Verify or quarantine older journal rows because cached results can return without another admission callback.

#### Reply to forms or cancel a task

Form replies require `allow_input_replies=True` and a vetted target that binds each request key to one immutable request for the task's lifetime. The target must reject retired or satisfied keys. Changed requests need new keys and fresh approval. Standard `tasks/update` cannot require a matching revision before applying an update. Supported forms contain flat strings, booleans, numbers and string lists, including choices. Other validation constraints are refused.

Use `advance()` for approved mutations:

- For a form reply, pass `operation="respond"` and `response=MCPInputReply(challenge=..., responses=...)` with host-approved `ElicitResult` values from `mcp_types`.
- For cancellation, pass `operation="cancel"`.

Both operations re-read the task before dispatch, even before its next poll time. Supply `reserve_mutation(claim, reply)` for either operation; cancellation passes `None`.

Immediately before sending, the host must atomically check current permissions, binding, generation, receipt, and approval. In that same operation, reserve all selected form keys or the task's single cancellation. Return exactly `True` only after the new reservation commits. Retries and lost acknowledgments do not release a reservation. After a crash, observe the task before deciding what remains unknown.

#### Callback timeouts and context

Initial authorization and each network exchange have separate timeout budgets. Cooperative host callback cleanup can exceed them. Authorization and admission callbacks run on the caller's loop, so keep callbacks short.

Each HTTP request uses a separate copy of the operation's context. Rebinding a `ContextVar` does not affect later requests, but mutable context values remain shared. Reset context tokens in the callback that created them.
