# Use ChatGPT plan usage with a registered integration

This integration excerpt requires a matching [SDK source build](../../CONTRIBUTING.md#build-the-sdk-from-source). Your application supplies the registered sign-in flow, protected credential record, and authenticated user binding; this file does not provide a standalone sign-in service. It applies to eligible integrations that already have a Sign in with ChatGPT registration and the user's consent for ChatGPT plan usage. [Registration guide](https://developers.openai.com/siwc/token-sharing-open-source) · [Self-hosted VM setup](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)

During sign-in, verify the ID token against OpenAI's issuer keys and the issued client ID. Check the token's expiry, nonce, and subject, and check the granted scopes. Store the verified identity and credentials from that exchange together in a protected record. After authenticating the current application user, supply a callback that returns a `SignInWithChatGPTCredentials` snapshot from that user's record. Authorize access to a saved conversation before supplying its state or resume ID to the SDK. [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)

```python
from collections.abc import Awaitable, Callable

from agents import Agent, NativeProvider, SignInWithChatGPT, SignInWithChatGPTCredentials


def make_agent(
    get_credentials: Callable[[], Awaitable[SignInWithChatGPTCredentials | None]],
    model: str,
) -> Agent:
    return Agent(
        instructions="Help with the team's development work.",
        model=model,
        provider=NativeProvider(
            auth=SignInWithChatGPT(get_credentials=get_credentials),
        ),
    )
```

Choose a Responses Lite model available to the selected account. The SDK captures one credential snapshot when you open a Session. During refresh, keep the verified identity with the protected record and replace its access and refresh tokens. The token endpoint can renew credentials without returning a new ID token. Open a new Session to use the renewed bearer with the same verified identity and restore the saved conversation. The SDK reports auth or plan usage failures without switching to API-key billing. [Model catalog](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) · [Token renewal](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions#refreshing-tokens)

Pause plan-backed requests when `NativeError.category` is `usage_limit_exceeded`. For an HTTP plan limit, the SDK also exposes the service code `subscription_sharing_usage_limit_exceeded` and the request ID when supplied. For `subscription_sharing_unsupported_capability`, use `NativeError.param` when available to locate and remove the unsupported request field. Your application decides whether to retry a temporary service error. [Error recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)

The route uses HTTP streaming with a Responses Lite model. You can use direct Python function tools with one Agent. A function call becomes eligible to run after the response completes successfully. If the stream ends first, the SDK drops the pending call before it can run or enter conversation history. The SDK excludes handoffs, NativeAgents, deferred tool loading, code mode, hosted tools, computer tools, web search, and Realtime on this route.

[Choose another SDK example](README.md).
