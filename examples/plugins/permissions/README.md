# Permissions for SDK function tools

An application policy for every SDK function on one agent, including tools from other plugins. By default, refusal. The app has been looking up grants for the signed-in user and sharing only the decision with `permissions()`.

## Offline example

Requirements — Python 3.10 or later and a local `openai-agents` build with `agents.plugins`. No model service or credentials.

Installation and execution from this directory, in the SDK's Python environment

```sh
python -m pip install --no-deps .
python -m agents_permissions.example
```

Expected output

```text
0 reads of R42 after approval and revocation.
```

In the [example](src/agents_permissions/example.py), the `records` plugin has been replacing `latest` with `R42`. The app has been simulating a review of the edited request, approving it and then removing the user's grant. On resume, the SDK has been checking again before calling the function.

## Connecting your policy service

| Where | Required setup |
| --- | --- |
| Each protected agent, including specialists and handoff targets | `from agents_permissions import permissions` and `permissions()` in `Agent.plugins`. No automatic inheritance. |
| `Runner.run(..., context=app)` | Tenant and user from the app's authentication service. The same identity throughout review and resume. No reliance on identity claims from the prompt or arguments. |
| `RunConfig(policy_context=resolve)` | For policy name `"permissions"`, a fresh lookup using the agent, user, tenant, `call.owner`, `call.namespace`, `call.name` and edited `call.arguments`. Separate handling for other policy names. |

| Exact return value from `resolve(agent, policy, call, app)` | Effect |
| --- | --- |
| `{"decision": "allow"}` | No objection. No override of other policies or approval requirements. |
| `{"decision": "review"}` | A separate approval for this call, even after `always_approve=True` on an earlier call. |
| Any other JSON object, including `{}`, or no resolver | Refusal. |
| Resolver exception or invalid context | Error before execution of this call. |

**Human review** — display of the exact tool and edited arguments before `state.approve(item)` or `state.reject(item)`. For saved approvals, app verification of the tenant, user, expiration and replay status before restoring the checkpoint.

## Limits

Coverage only for SDK functions with exactly inspectable arguments. No control over native Codex actions, hosted tools or local commands. Trusted Python code only.

At the protected service, a separate access check in the same transaction as the operation. No rollback after an effect.
