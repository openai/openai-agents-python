# Choose a task recipe

Use these recipes to test one application behavior at a time. Install the [SDK preview wheel](../../../docs/sdk/installation.md) and use example files from the same release. Activate that Python environment and run commands from the source archive or checkout root. Entries marked development-only require a [matching source build](../../../CONTRIBUTING.md#build-the-sdk-from-source) and example files from that checkout.

For example, run the typed-tools recipe without model credentials:

```sh
python -m examples.codex.recipes.typed_tools
```

Expect a JSON receipt for SKU `synthetic` with `accepted` equal to `2`. The native engine executes the Python callback and validates its output. A local scripted peer chooses the tool calls and answer, so this run checks application wiring rather than model quality.

For scripted recipes, allow direct access to `127.0.0.1` or add it to your proxy bypass list. Live recipes require `OPENAI_API_KEY` and can incur model charges. Use synthetic inputs when inspecting requests or writing result artifacts.

## Validate tools and control effects

For each file below, run `python -m examples.codex.recipes.MODULE`, replacing `MODULE` with the filename without `.py`.

| Module | Execution | Expected result |
| --- | --- | --- |
| [typed_tools](typed_tools.py) | Scripted | Coerces the input quantity to `2`, records one order line, and prints its validated receipt. |
| [inspect_requests](inspect_requests.py) | Scripted | Prints tool declarations, original and effective arguments, and the result after the application changes the limit from `3` to `2`. |
| [concurrent_tools](concurrent_tools.py) | Scripted | Two overlapping Sessions share one quota unit. Assertions check one reservation and one `full` result. |
| [safe_effects](safe_effects.py) | Scripted | Repeats a synthetic write with the same key and checks that the injected in-memory writer records one effect. |
| [oversized_tool_output](oversized_tool_output.py) | Development-only, scripted | Reduces large function results, retains full evidence in application data, and refuses an excessive row count. Prints measured request and evidence counts. |
| [oversized_mcp_output](oversized_mcp_output.py) | Development-only, scripted, with a local MCP peer | Keeps bounded summaries in history and full evidence in application memory. Checks rejection of an oversized summary. Requires `aiohttp`. |
| [prepared_request](prepared_request.py) | Scripted | Edits the next sampling request after a tool result while preserving the original tool validation and policies. Prints `The record is allowed.` |
| [accepted_output](accepted_output.py) | Development-only, scripted | Withholds rejected drafts and publishes only application-checked values in memory. See the [accepted-output guide](accepted-output.md). |

The fake writer in `safe_effects` deduplicates only inside one process. It does not supply durable idempotency. Likewise, scripted model replies still execute real Python callbacks; replace external writers before using scripted tests.

## Add memory, retrieval, and specialists

| Module | Execution | Expected result |
| --- | --- | --- |
| [application_memory](application_memory.py) | Scripted | Saves, recalls, and deletes one user-scoped fact across fresh Sessions. Removes its temporary SQLite database on exit. |
| [specialist_agents](specialist_agents.py) | Scripted | Calls a separately configured Agent through a Python tool and returns its typed catalog summary. |
| [decisions_routing](decisions_routing.py) | Development-only, scripted | Checks default and Decisions-based routing, permission changes, invalid choices, and timeouts across separate specialist Sessions. See the [Decisions guide](../decisions-guide.md#route-to-an-authorized-specialist). |
| [decisions_handoffs](decisions_handoffs.py) | Development-only, scripted | Offers one authorized native handoff before model submission and checks selection, denial, cancellation, and a model call to an unoffered target. |
| [retrieval](retrieval.py) | Live | Searches a tenant-scoped synthetic corpus and validates that the answer cites only source IDs retrieved in this run. |
| [image_input](image_input.py) | Live | Creates an inline red PNG and prints a validated color and confidence. The expected dominant color is `red`. |
| [programmatic_tools](programmatic_tools.py) | Live | Uses model-generated JavaScript to aggregate Python tool results. The expected paper-sales total is `50`. Prints tool-call metadata and recorded call items. |

A separate specialist Session does not inherit native child tracking or isolation. The application supplies its tools, context, and permissions. Retrieval source checks establish source membership, not whether a source supports every claim in the answer.

## Connect MCP servers and inspect transport retries

[local_mcp.py](local_mcp.py) launches the included [stdio catalog server](fixtures/catalog_mcp.py), asks a live model for the price of `PAPER`, and prints the answer. The synthetic catalog price is 5 dollars. The server uses only the standard library; the model request still requires credentials:

```sh
python -m examples.codex.recipes.local_mcp
```

[mcp_resources.py](mcp_resources.py) and [mcp_prompts.py](mcp_prompts.py) use an existing trusted streamable HTTP MCP endpoint. They print server data without opening a model turn. Replace the endpoint, resource URI, and prompt name with values from your server:

```sh
python -m examples.codex.recipes.mcp_resources http://127.0.0.1:8000/mcp 'documents://catalog'
python -m examples.codex.recipes.mcp_prompts http://127.0.0.1:8000/mcp summarize --argument language=Python
```

These commands do not start an MCP server. The examples omit endpoint authentication; add your server's required headers in application code before connecting to a protected endpoint. Resource lists return individual pages. Prompt listing collects the supported bounded inventory. Neither operation adds its returned content to model context.

[observed_transport_attempts.py](observed_transport_attempts.py) wraps a borrowed OpenAI client's HTTP transport. This development-only recipe requires a matching source build. It also needs `openai`, `httpx`, and `aiohttp` from the matching contributor environment. Its local peer returns HTTP 503, then HTTP 200, without live credentials:

```sh
python -m examples.codex.recipes.observed_transport_attempts
```

Expect two transport entries, two peer receipts, one completed model response, and one adapter invocation. These counters describe different boundaries. A transport entry alone does not prove a socket write, service receipt, or billing event.
