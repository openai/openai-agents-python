import asyncio
import inspect

from agents import (
    Agent,
    ModelRetrySettings,
    ModelSettings,
    RetryDecision,
    RunConfig,
    Runner,
    retry_policies,
)


def format_error(error: object) -> str:
    if not isinstance(error, BaseException):
        return "Unknown error"
    return str(error) or error.__class__.__name__


async def main() -> None:
    apply_policies = retry_policies.any(
        # On OpenAI-backed models, provider_suggested() follows provider retry advice,
        # including fallback retryable statuses when x-should-retry is absent
        # (for example 408/409/429/5xx).
        retry_policies.provider_suggested(),
        retry_policies.retry_after(),
        retry_policies.network_error(),
        retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
    )

    async def policy(context) -> bool | RetryDecision:
        raw_decision = apply_policies(context)
        decision: bool | RetryDecision
        if inspect.isawaitable(raw_decision):
            decision = await raw_decision
        else:
            decision = raw_decision
        if isinstance(decision, RetryDecision):
            if not decision.retry:
                print(
                    f"[retry] stop after attempt {context.attempt}/{context.max_retries + 1}: "
                    f"{format_error(context.error)}"
                )
                return False

            print(
                " | ".join(
                    part
                    for part in [
                        f"[retry] retry attempt {context.attempt}/{context.max_retries + 1}",
                        (
                            f"waiting {decision.delay:.2f}s"
                            if decision.delay is not None
                            else "using default backoff"
                        ),
                        f"reason: {decision.reason}" if decision.reason else None,
                        f"error: {format_error(context.error)}",
                    ]
                    if part is not None
                )
            )
            return decision

        if not decision:
            print(
                f"[retry] stop after attempt {context.attempt}/{context.max_retries + 1}: "
                f"{format_error(context.error)}"
            )
        return decision

    retry = ModelRetrySettings(
        max_retries=4,
        backoff={
            "initial_delay": 0.5,
            "max_delay": 5.0,
            "multiplier": 2.0,
            "jitter": True,
        },
        policy=policy,
    )

    # RunConfig-level model_settings are shared defaults for the run.
    # If an Agent also defines model_settings, the Agent wins for overlapping
    # keys, while nested objects like retry/backoff are merged.
    run_config = RunConfig(model_settings=ModelSettings(retry=retry))

    agent = Agent(
        name="Assistant",
        instructions="You are a concise assistant. Answer in 3 short bullet points at most.",
        # This Agent repeats the same retry config for clarity. In real code you
        # can keep shared defaults in RunConfig and only put per-agent overrides
        # here when you need different retry behavior.
        model_settings=ModelSettings(retry=retry),
    )

    print(
        "Retry support is configured. You will only see [retry] logs if a transient failure happens."
    )

    result = await Runner.run(
        agent,
        "Explain exponential backoff for API retries in plain English.",
        run_config=run_config,
    )

    print("\nFinal output:\n")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
