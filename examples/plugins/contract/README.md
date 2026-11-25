# Test an installed plugin

Use this kit to test one SDK function tool at a time. The helper checks that the agent offers the tool and that the expected text reaches the next model request. Your test checks what the tool reads or changes. The model's final answer is scripted, so it cannot prove that a write happened.

## Run the ticket test

Copy this directory outside the SDK and plugin source trees. Use a fresh Python 3.10+ environment with matching plugin-enabled `openai-agents` and `agents-ticket-desk` wheels installed. Keep the three Python files together. Run both commands from the copied directory.

```sh
python -m pip check
python -EsB test_ticket.py
```

Expect the package versions, `Python (ScriptedModel)`, and `OK`. An assertion or import error exits with a nonzero status. For test discovery, use `python -I -m unittest discover -s .`. The test uses no live inference or ticket service.

For a plugin-enabled SDK build with a compatible native Codex binding, run `python -EsB test_ticket.py --codex`. This uses the Codex provider, a temporary `CODEX_HOME`, and synthetic Responses from a local HTTP server. The server checks the client on each request. The test fails if the native route is unavailable. It covers SDK function tools only.

## Adapt the checks

In [test_ticket.py](test_ticket.py), replace the package import, distribution name, plugin factory, and test client with yours. Pass the tool name, arguments, and expected text to [`expect_tool_output`](plugin_contract.py). Use the returned model on your agent and the run config on each `Runner.run` call. Include any other plugins you use on that agent.

For a write, inspect the proposed call and check for effects before and after review. The ticket test gives two tenants the same ticket ID and checks four cases.

| Case | Calls to the desk | Writes | Text on the next turn |
| --- | --- | --- | --- |
| Approved for the owner | 1 | 1 for the owner | `closed` |
| Rejected | 0 | 0 | `declined` |
| Grant revoked after approval | 1 | 0 | `denied` |
| Other tenant uses the owner's agent | 0 | 0 | `denied` |

For a tool without review, remove the interruption and resume steps. Keep the check for effects and let the run continue to the second model turn.

## Check upgrades and rollbacks

Keep the host test unchanged. Run it with the current wheels, the proposed wheels, and the original wheels again. Use a fresh environment for each pair. Record the wheel hashes, Python version, route, and exit status. Repeat on each route you support.

These tests start fresh runs and resume in memory. If your app saves approvals, also test checkpoints across deployments. Bind each checkpoint to its authenticated owner and use a new `plugin_revision` when policy or tool behavior changes, including on rollback. Check that stale approvals cannot cause writes.
