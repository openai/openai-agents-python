# Models and authentication

Choose the model connection and credentials for Agent runs. Use an API key with the default Responses connection, connect a compatible custom endpoint, or supply an application-owned model client in a matching development build.

[Install the SDK](installation.md) before opening a Session. Complete the [first agent](index.md#run-your-first-agent) for the default API-key path. Examples with `await` or `async with` belong inside an async application unless the page labels a complete script.

| Task | Start here |
| --- | --- |
| Use an API key or custom endpoint | [Provider connection](#provider-connection) |
| Select a model or change request settings | [Model and Session settings](#model-and-session-settings) |
| Reuse an asynchronous model client | [Application-owned clients](#use-an-application-owned-model-client) |
| Reuse a saved ChatGPT login for local experiments | [Experimental Codex login](#reuse-a-codex-login-experimental) |
| Use an existing Sign in with ChatGPT registration | [Registered integration recipe](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/sign_in_with_chatgpt.md), for matching development builds |
| Share and close a provider | [Provider lifetime](#provider-lifetime) |

The decision provider used by `Agent.decide()` is separate from this model connection. Configure it through [typed decisions](configuration.md#make-typed-decisions).

## Provider connection

Pass `NativeProvider(base_url=..., api_key=...)` to configure the native Responses connection for model requests. The SDK uses Codex's built-in model client. The default transport accepts endpoint settings rather than an OpenAI client instance. Matching source builds also accept [application-owned model clients](#use-an-application-owned-model-client) through an adapter.

The default transport requires Responses support from custom endpoints. The [endpoint checks](providers.md) exercise that transport. The application-model route below supports Chat Completions separately.

API-key connections use these defaults.

- `base_url=None` sends model requests to `https://api.openai.com/v1`. The SDK does not read `OPENAI_BASE_URL`. To use it, import `os` and pass `base_url=os.environ["OPENAI_BASE_URL"]`.
- If you omit `api_key`, the SDK reads `OPENAI_API_KEY` when the Session opens. Keep credentials in current application configuration, outside saved conversation state.
- `response_mode="streaming"` delivers model progress as it arrives.
- In streaming mode, `supports_websockets=None` enables WebSockets for `base_url=None` and disables them for any explicit `base_url`. Set `True` for a compatible custom provider or `False` to require HTTP. When enabled, the native runtime selects the final transport for the provider and model.

The pinned Codex bypasses HTTP proxies with `CODEX_SANDBOX=seatbelt`. To prevent unintended direct requests, the SDK rejects Session startup if `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or their lowercase forms are nonempty in that mode. The check applies even with a `NO_PROXY` exemption or WebSockets enabled. Run outside that sandboxed environment, or remove unneeded proxy settings.

## Model and Session settings

Set `model` to a native model identifier, or omit it for the pinned runtime's default. `ModelSettings` supports `reasoning_effort`, `reasoning_summary` and `service_tier`. Omitted settings keep native defaults. `service_tier=None` clears that override. These settings grant no tool or workspace access.

When opening a Session:

- `agent.session(model=...)` overrides the Agent model.
- Supplied `model_settings` keys override Agent settings. Omitted keys keep the Agent or native defaults.
- [Portable restore](state.md#save-and-restore-a-portable-checkpoint) keeps the saved model and settings instead.

SDK version `0.24.0-alpha.7` or later also supports `tool_choice` in `ModelSettings`, for example `model_settings={"tool_choice": "auto"}`. The default is `"auto"`. `"auto"` lets the model choose whether to call tools; `"none"` disables model tool calls; `"required"` requires a call to an offered tool. These string modes do not select a specific tool or add tool permissions. Use the [matching preview wheel](installation.md) for this option.

An open Session keeps its captured configuration. Change instructions or native model settings with the idle [Session update methods](sessions.md#update-tools-instructions-and-model-settings). Another Agent cannot change it. Sessions opened with `NativeAgents` keep these settings fixed.

### Additional controls in development builds

The controls in this section require a [build from matching development source](installation.md#use-development-branch-examples). The alpha.7 wheel does not include these controls.

Pass these fields through `model_settings` on the Agent, when opening a Session, or in an idle `session.update(model_settings=...)`. Omitted keys preserve the accepted settings. `None` clears the corresponding SDK override. Providers still determine which settings their models support and which numeric ranges they accept.

| Setting | Value and effect |
| --- | --- |
| `max_output_tokens` | An integer from `0` through `2**64 - 1` for the output cap on an ordinary model request. Internal compaction and reviewer requests do not use this cap. This is separate from a run's total usage budget. |
| `temperature`, `top_p` | Finite numbers forwarded as sampling controls. The provider owns their supported ranges and combinations. |
| `top_logprobs` | An integer from `0` through `255`, requesting token alternatives where the provider supports them. |
| `parallel_tool_calls` | A bool controlling model-side parallel tool calls. It does not set Python callback concurrency. |
| `verbosity` | `"low"`, `"medium"`, or `"high"`, forwarded as response verbosity. |
| `metadata` | A mapping of strings to strings attached to model requests. Keep credentials and private application data out of this mapping. |
| `store` | A bool requesting provider-side response storage. `True` requires API-key authentication. Provider policy determines retention. |
| `prompt_cache_retention` | `"in_memory"` or `"24h"`. |
| `prompt_cache_ttl` | `"30m"` or `"12h"`, sent as `prompt_cache_options.ttl`. TTL and retention are independent; omitting one does not set the other. The provider determines cache behavior. |
| `invocation_timeout_ms` | A positive integer through `4_294_967_295` setting a local deadline for each model invocation, including its internal waits and retries. |

For example, this declaration requests a response cap and a local invocation deadline. It starts no model work:

```python
from agents import Agent

agent = Agent(
    instructions="Answer briefly.",
    model="gpt-5.5",
    model_settings={"max_output_tokens": 512, "invocation_timeout_ms": 30_000},
)
```

Use a standard Responses model for the additional wire controls; known Responses Lite combinations fail locally rather than silently dropping settings. A provider rejection remains a failed request. The SDK does not remove a rejected setting and replay the work.

An invocation deadline raises `NativeError` with `code="model_invocation_deadline_exceeded"`. It is separate from an application deadline for an entire turn: Python tool execution and application waits can outlast a model invocation. Cleanup can also finish after the deadline. An accepted terminal response disarms that invocation's clock before Python result settlement. Check earlier tool effects before retrying. To limit the whole wait, see [bound a wait and stop owned work](troubleshooting.md#bound-a-wait-and-stop-owned-work).

Development builds also accept a `ToolRef` for `tool_choice`, such as a declared function tool's `tool.ref`. This narrows sampling to one currently admitted eager tool and requires it. It cannot add a tool, restore one removed by `tools` or `before_step`, or select a deferred tool. Set `tool_choice="auto"` or `None` to clear a named target. A required choice can request the same tool on later sampling steps, so bound repeated work with a [run budget](observability.md#limit-a-runs-model-requests-and-usage) or change the selection when idle.

## Use an application-owned model client

Matching [source builds](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md#build-the-sdk-from-source) accept `NativeProvider(model_backend=...)`. The `0.24.0-alpha.7` wheel does not include this option. The native runtime retains ownership of the tool loop and conversation history while the application supplies each completed model response.

Build and install a wheel from the same checkout. The script also needs the `openai` package for `AsyncOpenAI`; install that client with `python -m pip install openai`. Set `OPENAI_API_KEY`, then save this complete script as `borrowed_client.py` and run `python borrowed_client.py`. The script uses an asynchronous OpenAI client through `ChatCompletionsModel` and makes live model requests that can incur charges.

```python
import asyncio

from openai import AsyncOpenAI

from agents import Agent, ChatCompletionsModel, NativeProvider


async def main() -> None:
    async with AsyncOpenAI() as client:
        async with NativeProvider(
            model_backend=ChatCompletionsModel(client),
        ) as provider:
            agent = Agent(
                instructions="Answer briefly.",
                model="gpt-4.1",
                provider=provider,
            )
            result = await agent.run("Reply with atlas.")
            print(result.final_output)


asyncio.run(main())
```

The application owns the client and its credentials. The nested context managers close the one-shot Session, then the provider, then the borrowed client. `ChatCompletionsModel` requests one complete response per model step rather than token streaming. The application chooses its endpoint through `AsyncOpenAI(base_url=...)` when needed.

Other public adapters support the following clients:

| Adapter from `agents` | Borrowed client |
| --- | --- |
| `ResponsesModel(client)` | An asynchronous client with `responses.create`. |
| `LitellmModel(client, model=...)` | An installed LiteLLM module or compatible object with `acompletion`. |
| `AnyLLMModel(client, model=...)` | An application-created AnyLLM provider with `acompletion`. |

Applications also implement `NativeModel.complete(request)` or `ModelProvider.get_model(model_name)` directly. `ModelRequest.body` carries the native request, and `ModelResponse` carries completed Responses-format output items and optional usage counters. The [model reference](reference.md#application-models) describes these signatures.

This route uses the HTTP-mode native loop and rejects `supports_websockets=True`, `CodexLogin`, and native child agents configured through `NativeAgents`. Cancellation waits for the model callback to finish and discards its late response. The runtime does not fall back to its built-in HTTP client after an adapter failure. Providers determine which request settings they support; the adapters reject unsupported conversions rather than silently replaying work. Check your required tools, output formats, and model controls before using an adapter in your application.

## Buffer model responses

Use `NativeProvider(response_mode="buffered")` when the endpoint supports nonstreaming Responses and your application can wait for each complete model response. The SDK requests JSON over HTTP and delivers that response's output events after reading its full body. Tool calls start after that model response completes. Tool events remain available while tools execute, and subsequent model requests follow the normal tool loop.

Each buffered HTTP response body is limited to 64 MiB and one million structural characters (`{}[],:`) outside strings. Exceeding either limit fails without retry. Use streaming mode for larger responses.

Set the agent's `model` explicitly to a model that uses standard Responses. The current default model uses Responses Lite and cannot use buffered mode.

If the provider reports failure or an incomplete response that cannot continue, such as a token-limit error, that response's output is not delivered. An undecodable response body also yields no partial output. Any validated response metadata remains in the turn record.

Buffering avoids processing individual streaming fragments. The option applies to every model request in sessions opened with that provider, including restored sessions. Saved state does not choose the mode; the current provider does. Repeat the [endpoint checks](providers.md) with `response_mode="buffered"` for the features your application uses.

Buffered mode requires API-key authentication. It does not support `CodexLogin`, Responses Lite, or native child agents declared with `NativeAgents`. An explicit `supports_websockets=True` conflicts with buffered mode. Unsupported combinations fail before model dispatch; the SDK does not retry the request as a stream.

## Reuse a Codex login (experimental)

Pass `NativeProvider(auth=CodexLogin())` to reuse an existing Codex-managed ChatGPT OAuth login. API-key authentication remains the default when `auth` is omitted.

!!! warning "Local experimental use only"
    This feature may change or be removed. The public beta and general availability of the Agents SDK may ship without this feature. Follow the official [Codex authentication guidance](https://learn.chatgpt.com/docs/auth) and [application authentication policy](https://learn.chatgpt.com/docs/app-server#auth-endpoints) for permitted uses.

Complete `codex login` separately before opening an SDK Session. This mode reads the ChatGPT login saved in `auth.json`. If Codex uses the operating system credential store, configure file storage and sign in again. See [Codex credential storage](https://learn.chatgpt.com/docs/auth#credential-storage) to configure file storage. Protect the login file and keep its tokens out of logs and source control.

`CodexLogin()` looks in `CODEX_HOME` if set and in `~/.codex` otherwise. Pass `CodexLogin(codex_home=...)` to select another existing Codex directory. Pass `account_id=...` to require the expected ChatGPT workspace ID. The Session fails to open if the saved login has a different workspace ID or has no workspace ID.

### Set up a login on a headless host

1. Ask your ChatGPT workspace admin to enable device-code sign-in in the workspace permissions. For a personal account, enable it in your ChatGPT security settings.
2. On the headless host, run `codex login --device-auth`. Open the displayed link in a browser on another device and enter the one-time code. Use the ChatGPT account and workspace you want the SDK to use.
3. Make sure the CLI saved `auth.json` in the directory that `CodexLogin` will read. If your setup uses a credential store, see the [file storage settings](https://learn.chatgpt.com/docs/auth#credential-storage).

If device-code sign-in is unavailable, use a copied login if your workspace permits file storage. Sign in with `codex login` on a machine with a browser and file storage enabled. Then [copy `auth.json` over a secure connection](https://learn.chatgpt.com/docs/auth#fallback-authenticate-locally-and-copy-your-auth-cache) to the Codex directory on the headless host. Use the default directory or set `CodexLogin(codex_home=...)` to the destination directory. Give the user running the SDK write access to that directory and restrict access to the cache file to that user. Treat the file like a password and keep it out of source control, logs, and shared artifacts.

Sessions in one SDK process can share the same saved login. The SDK coordinates token refresh in that process and writes renewed credentials back to the file. Keep the updated file so the next process can use it after a restart. Use a single process at a time to refresh a saved login, even if copies of the file exist on other hosts.

### Run a coding turn without an API key

This complete script makes a live model request using the saved login. Run it with an SDK build that includes this experimental API:

```python
import asyncio

from agents import Agent, CodexLogin, NativeProvider


async def main() -> None:
    async with NativeProvider(auth=CodexLogin()) as provider:
        agent = Agent(
            instructions="Write concise Python code with unit tests.",
            provider=provider,
        )
        async with agent.session() as session:
            account = session.codex_account
            if account is None:
                raise RuntimeError("Expected a saved Codex login")
            print("Saved workspace ID:", account.account_id)
            print("Saved plan:", account.plan_type)
            turn = await session.submit(
                "Write an is_prime(n: int) -> bool function and three unit tests."
            )
            result = await turn.get_result()
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Save the script as `coding_turn.py`. On macOS or Linux, run it with both API-key variables unset.

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY python coding_turn.py
```

The example asks for code and tests in the answer. To execute commands or edit workspace files, configure an [execution Environment](execution.md).

### Account identity, refresh, and usage

`session.codex_account` returns a `CodexAccount` with `account_id` and `plan_type` for a Session opened with `CodexLogin`, or `None` in API-key mode. Check `account_id` to identify the saved ChatGPT workspace before submitting work. Some personal logins have no workspace ID and report `account_id=None`. Pass the expected ID to `CodexLogin(account_id=...)` to check it when opening the Session. The saved `plan_type` may be `"unknown"`. For actual usage on that account, check the [Codex usage dashboard](https://chatgpt.com/codex/settings/usage).

The native runtime refreshes the saved login for the same user and workspace. Portable restore and local conversation resume must use that same identity. If you switch users or workspaces, start a new conversation. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

This mode uses HTTPS on the standard Codex backend. Workspaces that require a separate backend need their own integration. Omit `api_key` and `base_url`, and leave `supports_websockets` unset or set it to `False`. If the saved login fails, the SDK stops even when API keys or other credentials are set.

## Read Codex plan usage

Matching development builds support `plan_usage()` and its UTC reset datetimes. The `0.24.0-alpha.7` preview does not include this API. The [source-build guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/CONTRIBUTING.md#build-the-sdk-from-source) provides setup for the matching source commit.

`await session.plan_usage()` requests a fresh usage snapshot through the Session's saved `CodexLogin`. API-key Sessions return `None` without an account request. Each call reads current provider data rather than reusing the previous snapshot. `observed_at` tracks the snapshot receipt time. Applications choose how long to reuse the snapshot before another read.

Inside an open Session, this fragment shows each reported primary-window reset:

```python
usage = await session.plan_usage()
if usage is None:
    print("API-key Session: no Codex plan snapshot")
else:
    print("Observed:", usage.observed_at)
    print("Identity verified:", usage.identity_verified)
    print("Included usage allowed:", usage.ordinary_usage_allowed)
    print("Available window resets:", usage.reset_credits_available)
    for limit in usage.limits:
        window = limit.primary
        if window is not None and window.resets_at is not None:
            print(limit.limit_id, window.used_percent)
            print("Time until reset at observation:", window.resets_at - usage.observed_at)
```

Window `resets_at`, individual spend-limit `resets_at`, and `observed_at` use timezone-aware UTC datetimes. Missing windows, credits, and optional fields represent unknown data. `CodexPlanSpendLimit.limit`, `CodexPlanSpendLimit.used`, and `CodexPlanCredits.balance` preserve the provider's strings. Additional usage buckets appear in `limits` alongside the main Codex bucket.

`account_id` names the Session's saved workspace when available. `identity_verified=True` indicates that the response identified both the saved account and user. Omitted identity fields leave verification false; conflicting identities raise `RuntimeError`. `ordinary_usage_allowed` reports the provider's evidence for included usage only with verified identity, and otherwise remains `None`. `reset_credits_available` counts available window resets rather than credit balance.

These values serve as scheduling hints before `submit()`. The provider decides admission for each turn; the snapshot offers no reservation of capacity. Failed account reads raise `RuntimeError`. Reads use the Session's cached credentials. After token expiry, applications renew the saved login and open a new Session before retrying. Closing the Session cancels pending reads. The [Codex usage dashboard](https://chatgpt.com/codex/settings/usage) provides a separate account view.

## Read API-key rate limits

Read `session.rate_limits()` for the latest snapshot from a response with usable rate-limit data. The snapshot starts with unknown values. Each reported window can contain a limit, a remaining count, and the number of seconds until reset as measured at the response. `requests`, `tokens`, and `project_tokens` can each be absent. The provider can also supply a retry delay or an absolute retry time. Treat every `None` as unknown.

For live updates, observe `ApiRateLimitsUpdated` from `agents.observation`. Use its `snapshot` field or read the current Session snapshot after an observation gap. Rate-limit updates can arrive independently of token usage. These snapshots describe the API-key connection. Account plan limits, credits, and billing are outside their scope.

Read a settled failure with [`wait_for_attention()`](sessions.md#wait-for-a-decision-or-a-result). A reported temporary rate limit yields `reason="rate_limited"`. A reported blocking limit, such as exhausted quota, yields `reason="quota_blocked"`. Base that distinction on the failure reason. A remaining count alone cannot tell you whether the next request will succeed.

## Provider lifetime

Each Session captures provider settings and owns its native connections. If neither the Agent nor the Session supplies a provider, the Session creates its own and attempts to close it automatically. Reusing an Agent does not share default providers.

To share a provider, pass the same `NativeProvider` to the Agents or Sessions. After all its Sessions close, call `await provider.aclose()`. Closing while a Session is open or opening raises an error. A successful close prevents reuse. Create a new provider for later Sessions.

## Recover a failed managed-environment opening

If opening a [managed Environment](execution.md#manage-the-executor-lifetime) fails and automatic cleanup cannot finish, the exception can carry the implicit provider in `error.provider`. It may still own an executor manager or native resources. Check this optional attribute and retry `aclose()` before discarding the owning event loop.

This applies only to opening failures from `agent.session()`, `agent.run()` and `agent.run_sync()`. An explicit provider remains application-owned and is not attached to the error. Close it after its other Sessions close.

This fragment uses your `agent` and the linked example's `executor_lease()` manager:

```python
from agents import NativeProvider

try:
    result = await agent.run("Inspect the workspace.", environment=executor_lease())
except BaseException as error:
    recovery = getattr(error, "provider", None)
    if isinstance(recovery, NativeProvider):
        try:
            await recovery.aclose()
        except BaseException as cleanup_error:
            raise error from cleanup_error
    raise
```

Run async cleanup on the failed opening's event loop. If cleanup fails again, this handler keeps the opening error and its provider, with the cleanup error as its cause. Retain the provider and retry cleanup before discarding the loop.

After `run_sync()` fails, a caller outside an event loop can use `asyncio.run(recovery.aclose())`. The provider routes cleanup to the retained run loop.
