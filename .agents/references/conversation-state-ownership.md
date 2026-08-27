# Conversation State Ownership

Use this reference for changes involving multi-turn input, sessions, `conversation_id`, `previous_response_id`, `auto_previous_response_id`, compaction, retries, `call_model_input_filter`, or `RunState` resume.

## Choose One Conversation Strategy

The state owner determines what the next model request should contain.

| Strategy | State owner | Next-turn input |
|---|---|---|
| Explicit replay with `result.to_input_list()` | Application | Replay-ready history plus the new turn |
| SDK session | Application storage plus the SDK | The same session plus the new turn |
| `conversation_id` | OpenAI Conversations API | The same conversation ID plus only the new turn |
| `previous_response_id` or `auto_previous_response_id` | OpenAI Responses API | The previous response ID plus only the new turn |
| `RunState` resume | Serialized Agents SDK run | Resume the same interrupted run; this is not a new conversation strategy |

In normal use, select one conversation strategy. Mixing client-managed replay or sessions with server-managed continuation can duplicate context unless the implementation explicitly reconciles both owners. Read [Session persistence](session-persistence.md) for the client-managed storage contract.

## Server-Managed Continuation

- `OpenAIServerConversationTracker` in `src/agents/run_internal/oai_conversation.py` owns delta calculation for `conversation_id`, `previous_response_id`, and `auto_previous_response_id`.
- Send only items that the server has not already acknowledged. Object identity is useful only within one process; resume and retry paths also require stable item IDs, tool call IDs, and content fingerprints.
- Update `previous_response_id` from the most recent response that actually has an ID. Do not erase a valid chain because an adjacent provider response lacks one.
- Session persistence cannot be combined with server-managed continuation. `validate_session_conversation_settings()` rejects a session with `conversation_id`, `previous_response_id`, or `auto_previous_response_id`; do not add a second history writer without defining reconciliation and dedupe semantics.
- Treat `conversation_id` and `previous_response_id` / `auto_previous_response_id` chaining as mutually exclusive state owners.

## Filters, Retries, and Resume

- `call_model_input_filter` runs on the prepared model payload. With server-managed continuation, that payload may already be a new-turn delta rather than full history.
- The filter must return `ModelInputData` with list input. Mark exactly the returned list as sent immediately before the request so nested preparation cannot add unsent items, rewind that tracking before retrying a failed request, and preserve it after success.
- Keep streaming and non-streaming tracker updates aligned. Both paths must preserve the same delta, retry, and response-ID semantics.
- Stateful retries require replay-safety evidence. Do not blindly resend a request that may already have advanced server state.
- `RunState` persists conversation identifiers and reconstructs tracker knowledge for resumed runs. Resume must not replay acknowledged input, lose unsent tool outputs, or increment the turn count without a model call.
- Conversation continuation carries context into a new turn. `RunState` resume continues a paused run. Do not substitute one mechanism for the other.

## Compaction

- `compaction_mode="previous_response_id"` depends on a usable stored response chain.
- `compaction_mode="input"` rebuilds from client-held items and is the fallback when the server chain is unavailable or `store=False` prevents later response lookup.
- Compaction must preserve the chosen state owner. Do not compact from local history and then also replay that history through server-managed continuation.

## Handoffs

- Server-managed conversations send deltas, so handoff input filters are not supported. `Handoff.input_filter` and `RunConfig.handoff_input_filter` should raise instead of rewriting a history the server already owns.
- `nest_handoff_history` is a client-history transformation. When server-managed continuation is active, disable it with a warning and continue with delta-only input.
- Keep generated items and session items distinct during handoff processing. The next model input may be filtered, but session history needs the full unfiltered item sequence when client-managed sessions are active.

## Review Checklist

1. Name the state owner before changing request construction.
2. Specify whether the model receives full history or a delta on every affected path.
3. Verify first turn, follow-up turn, retry, interruption, serialized resume, and streaming behavior.
4. Test tool calls and outputs separately; call IDs and output fingerprints have different dedupe roles.
5. Confirm that filtering, compaction, and session persistence do not introduce a second source of truth.

## Sources

- [OpenAI conversation state guide](https://developers.openai.com/api/docs/guides/conversation-state)
- [OpenAI running agents guide](https://developers.openai.com/api/docs/guides/agents/running-agents#choose-one-conversation-strategy)
- `src/agents/run_internal/oai_conversation.py`
- `src/agents/run_internal/run_loop.py`
- `src/agents/run_internal/session_persistence.py`
- `src/agents/run_state.py`
- `docs/running_agents.md`
- `docs/sessions/index.md`

Recheck the official API reference with `$openai-knowledge` before changing server-managed continuation behavior.
