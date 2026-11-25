# Permissions for SDK function tools

Use the [v0 plugin preview wheel](../README.md#install-the-preview) in the same environment as this package.

Add `permissions()` to an agent to refuse SDK function calls by default, including calls to other plugins' tools. Look up grants in the host app and share only the decision with the plugin.

## Offline example

Requirements — Python 3.10 or later and a local `openai-agents` build with `agents.plugins`. No model service or credentials.

Installation and execution from this directory, in the SDK's Python environment

```sh
python -m pip install .
python -m agents_permissions.example
```

Expected output

```text
0 reads of R42 after approval and revocation.
```

In the [example](src/agents_permissions/example.py), the `records` plugin has been replacing `latest` with `R42` through the experimental `prepare_tool` hook. The app has been simulating a review of `R42`, approving the call and then removing the user's grant. On resume, the SDK has been checking the grant again and refusing the call.

## Connecting your policy service

| Where | Required setup |
| --- | --- |
| Each protected agent, including specialists and handoff targets | `from agents_permissions import permissions` and `permissions()` in `Agent.plugins`. No automatic inheritance. |
| `Runner.run(..., context=app)` | Tenant and user from the app's authentication service. The same identity throughout review and resume. No reliance on identity claims from the prompt or arguments. |
| `RunConfig(policy_context=resolve)` | For policy name `"permissions"`, a fresh lookup using the agent, user, tenant, `call.owner`, `call.namespace`, `call.name` and edited `call.arguments`. Separate handling for other policy names. |

Use `app`, the fourth argument to `resolve`, for the signed-in user's identity. Use `call.owner` for the name of the plugin supplying the tool (`None` for an agent's own tool). The SDK has been passing the resolver's return value to the policy as `call.context`. Use `async def resolve(...)` for an asynchronous client. Keep lookups read-only and safe to repeat before dispatch and on resume.

| Exact return value from `resolve(agent, policy, call, app)` | Effect |
| --- | --- |
| `{"decision": "allow"}` | No objection. No override of other policies or approval requirements. |
| `{"decision": "review"}` | A separate approval for this call, even after `always_approve=True` on an earlier call. |
| `{"decision": "deny"}`, any other JSON object, or no resolver | Refusal, including an `allow` or `review` object with extra fields. |
| Resolver exception or invalid context, such as `None` or a string | Error before execution of this call. |

Show the reviewer the signed-in tenant and user, exact tool and final arguments. Then call `state.approve(item)` or `state.reject(item)`. Before a live resume, check the login again. Pass the original agent and omit `context` to reuse the original application object.

## Saving a single-agent approval

**Check the owner before restoring an approval.** The SDK has been omitting the application context from saved plugin runs. Consider two tenants with the same record ID and grants requiring review. Loading one tenant's approval with the other's context has been allowing the read without a fresh review.

Save the following as `saved_read.py` and run `python -I saved_read.py` with this package installed. Expect `1 read of R42.` The example has been simulating both the review and private server storage.

```python
import asyncio
import json
from secrets import token_urlsafe

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, function_call
from agents_permissions import permissions
from agents_permissions.example import Records, records, resolve, resume_once


async def main():
    app = Records("acme", "pat", {("acme", "pat", "R42"): "review"})
    call = function_call("read_record", {"record_id": "latest"}, call_id="one")
    agent = Agent(
        name="Desk",
        model=ScriptedModel([[call]]),
        plugins=[permissions(), records],
        tool_use_behavior="stop_on_first_tool",
    )
    config = RunConfig(policy_context=resolve, plugin_revision="desk-v1", tracing_disabled=True)
    paused = await Runner.run(agent, "Read the latest record.", context=app, run_config=config)
    (item,) = paused.interruptions
    assert item.tool_name == "read_record"
    assert item.arguments and json.loads(item.arguments) == {"record_id": "R42"}
    state = paused.to_state()
    state.approve(item)
    request_id = token_urlsafe()
    saved = {(app.tenant, app.subject, request_id): state.to_string()}

    signed_in = Records(tenant="acme", subject="pat", grants=app.grants)
    await resume_once(agent, signed_in, saved, request_id, config)
    assert signed_in.reads == ["R42"]
    print("1 read of R42.")


asyncio.run(main())
```

The app's [`resume_once`](src/agents_permissions/example.py) has been removing the matching entry before calling `RunState.from_string`. See [the test](../../../tests/test_reference_permissions.py) for a different tenant, a different user and a replay.

### Production requirements

- Keep snapshots in trusted server storage. Give the client only a unique request ID. Read the tenant and user from the authenticated session on every request.
- Match the owner and request ID, check expiry and consume the approval in one database transaction. Leave the approval intact on an owner mismatch.
- Set `plugin_revision` from trusted deployment configuration before the first run. Change it and require a fresh review after changing the tools or policy. Never use the revision as proof of ownership.
- Plan for failure after consumption. Before retrying a write, check the service for a completed write and require fresh approval.

## Limits

Coverage only for SDK functions with exactly inspectable arguments. No control over native Codex actions, hosted tools or local commands. Trusted Python code only.

At the protected service, check current access in the same transaction as the operation. Never rely on a later refusal to undo an earlier effect.
