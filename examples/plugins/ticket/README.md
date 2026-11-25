# Ticket Desk

A support-ticket plugin for the OpenAI Agents SDK preview. One tool, `close_ticket(ticket_id: str)`, with per-call approval and an application-supplied client.

## Try a synthetic ticket offline

Start with Python 3.10+ and a trusted, plugin-enabled SDK installation. Plain released v0 wheels lack the preview API. Install only trusted packages and build dependencies. Plugin code runs with the application's permissions.

In that environment, from this package directory

```sh
python -c "from agents.plugins import Plugin"
python -m pip install --no-deps .
python -I -m agents_ticket_desk
```

Installation may download build dependencies. The demo needs no API key and sends no requests. Type `yes` at the review prompt for `T-1 closed`. Any other answer leaves the ticket open. Each launch starts with a fresh in-memory ticket.

Run the offline approval and grant checks from the same directory.

```sh
python -I -m unittest discover -s tests
```

## Connect your client

See the [complete runnable demo](agents_ticket_desk/__main__.py) for the client, review prompt and provider setup. To wire in your own client, start with

```python
from agents import Agent
from agents_ticket_desk import ticket_desk


def make_desk(client, signed_in_user):
    return Agent(
        "Desk",
        plugins=[ticket_desk(client, owner=signed_in_user)],
        tool_use_behavior="stop_on_first_tool",
    )
```

- Use a trusted, unique user ID, including tenant scope where needed. Create a separate desk for each owner. Pass that ID as `context` on the first run. The tool accepts only the ticket ID.
- Implement `async def close_if_allowed(self, user: str, ticket_id: str) -> bool`. At write time, verify the authenticated session still belongs to the user and check the current close grant for that exact ticket. Make both checks and the write one atomic operation. Return `True` for success and `False` for refusal. Keep credentials and session state separate for concurrent users.
- Show the reviewer the owner, action and final ticket ID. Call `state.approve(item)` or `state.reject(item)` for that call only. Never use sticky approval.
- Before live resume, verify the login still matches the original owner. Resume with `await Runner.run(desk, state, run_config=config)` to retain the original context object. On a user switch, stop and start a new proposal with a fresh review.

## Protect saved approvals

Use a trusted `RunConfig(plugin_revision=revision)` from the first run. Store the original owner and `state.to_string()` together in access-controlled, integrity-protected application storage. Before restoring, authenticate the record and the current user. Never accept a checkpoint or owner field directly from a caller.

**Check ownership before loading the state.** Rebuilding Bob's desk and loading Alice's approval with Bob's context could let Bob close the ticket without his own review if both users have grants and the revision matches.

```python
from agents import RunState


async def restore_for_user(agent, record, signed_in, revision):
    owner, snapshot = record
    if not signed_in or signed_in != owner:
        raise PermissionError("Checkpoint owner mismatch")
    return await RunState.from_string(
        agent, snapshot, context_override=signed_in, plugin_revision=revision
    )
```

Use the owner's desk and the current deployed revision. Change the revision when tools or policy change. On an ownership or revision mismatch, require a new proposal and approval. Prevent checkpoint replay in your application and make the backend operation idempotent. See the [offline tests](tests/test_tickets.py) for same-revision users and revoked grants.

## Opt in to a live provider

**Both commands send requests and may incur charges.** The ticket store stays local. A live response may omit the proposal. The demo stops unless it receives exactly one proposal to close T-1.

| Provider | Prerequisite | Command |
| --- | --- | --- |
| Python default | Configured `OPENAI_API_KEY` | `python -I -m agents_ticket_desk --openai` |
| Codex | Plugin-enabled SDK with the matching native binding and a configured Codex login | `python -I -m agents_ticket_desk --codex` |

The Codex path passes `CodexProvider` through `RunConfig(model_provider=provider)` and closes it in `finally`. Run it only in an isolated workspace with trusted Codex configuration and no sensitive files or external tools. Native commands can read files even with the demo's read-only sandbox. The ticket prompt covers only `close_ticket`.
