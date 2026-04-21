"""Example: retrying on HTTP 429 rate-limit errors.

``retry_policies.rate_limit()`` is a ready-made helper that retries on HTTP 429
responses only.  When the server includes a ``Retry-After`` or
``Retry-After-Ms`` header the runner waits exactly that long before the next
attempt.  If no header is present the runner falls back to the configured
backoff schedule (exponential by default).

For deeper provider integration you can combine it with
``retry_policies.provider_suggested()`` so the runner also honours explicit
provider retry advice and safety approvals.
"""

import asyncio

from agents import Agent, ModelRetrySettings, ModelSettings, RunConfig, Runner, retry_policies


async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")

    # Minimal: retry up to 4 times on HTTP 429, obeying any Retry-After header.
    rate_limit_policy = retry_policies.rate_limit()

    result = await Runner.run(
        agent,
        input="Say hello.",
        run_config=RunConfig(
            model_settings=ModelSettings(
                retry=ModelRetrySettings(
                    max_retries=4,
                    policy=rate_limit_policy,
                )
            )
        ),
    )

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
