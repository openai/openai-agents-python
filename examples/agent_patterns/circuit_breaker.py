"""Circuit breaker pattern for agents.

A circuit breaker wraps repeated agent calls and stops trying after a
configurable number of consecutive failures.  This prevents a misbehaving
or unreachable model from hammering the API and running up costs.

States:
- CLOSED  — normal operation; failures are counted.
- OPEN    — too many consecutive failures; calls are rejected immediately
            without hitting the API.
- HALF_OPEN — after a cooldown period the circuit lets one call through as
              a probe.  Success resets the breaker to CLOSED; failure puts
              it back to OPEN.

This pattern is useful when:
- You are running many parallel agent tasks and want fast-fail on outages.
- You have cost controls and cannot afford unlimited retries.
- You are integrating with unreliable external tools or APIs.

Run with:
    python -m examples.agent_patterns.circuit_breaker
"""

import asyncio
import time
from enum import Enum, auto

from agents import Agent, ModelBehaviorError, Runner, function_tool


# ---------------------------------------------------------------------------
# Circuit breaker implementation
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted while the circuit breaker is OPEN."""

    def __init__(self, failure_count: int, cooldown_remaining: float) -> None:
        self.failure_count = failure_count
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"Circuit breaker is OPEN after {failure_count} consecutive failures. "
            f"Cooldown: {cooldown_remaining:.1f}s remaining."
        )


class AgentCircuitBreaker:
    """Circuit breaker that wraps ``Runner.run`` calls for a single agent.

    Args:
        agent: The agent to protect.
        failure_threshold: Number of consecutive failures before the circuit opens.
        cooldown_seconds: How long to wait in OPEN state before probing again.
    """

    def __init__(
        self,
        agent: Agent,
        failure_threshold: int = 3,
        cooldown_seconds: float = 10.0,
    ) -> None:
        self._agent = agent
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - (self._opened_at or 0)
            if elapsed >= self._cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def _cooldown_remaining(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._cooldown_seconds - (time.monotonic() - self._opened_at))

    async def run(self, task: str) -> str:
        """Run the agent through the circuit breaker.

        Args:
            task: The user message to send to the agent.

        Returns:
            The agent's final output string.

        Raises:
            CircuitBreakerOpen: If the circuit is currently OPEN.
            Exception: Any exception from the underlying agent run (in CLOSED
                or HALF_OPEN state), after the failure counter is updated.
        """
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitBreakerOpen(
                failure_count=self._consecutive_failures,
                cooldown_remaining=self._cooldown_remaining(),
            )

        if current_state == CircuitState.HALF_OPEN:
            print("[circuit] HALF_OPEN — sending probe request...")

        try:
            result = await Runner.run(self._agent, task)
            self._on_success()
            if current_state == CircuitState.HALF_OPEN:
                print("[circuit] Probe succeeded — circuit CLOSED.")
            return result.final_output
        except Exception:
            self._on_failure()
            if self._state == CircuitState.OPEN:
                print(
                    f"[circuit] Opened after {self._consecutive_failures} consecutive failures."
                )
            elif current_state == CircuitState.HALF_OPEN:
                print("[circuit] Probe failed — circuit remains OPEN.")
            raise


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


@function_tool
def lookup_order(order_id: str) -> str:
    """Look up the status of an order.

    Args:
        order_id: The order identifier.
    """
    statuses = {
        "ORD-001": "Shipped — expected delivery Jun 25",
        "ORD-002": "Processing",
        "ORD-003": "Delivered",
    }
    return statuses.get(order_id, f"Order {order_id} not found.")


support_agent = Agent(
    name="Support Agent",
    instructions="You are a customer support agent. Use the lookup_order tool to answer order status questions.",
    tools=[lookup_order],
)


async def main() -> None:
    breaker = AgentCircuitBreaker(
        agent=support_agent,
        failure_threshold=2,
        cooldown_seconds=5.0,
    )

    tasks = [
        "What is the status of order ORD-001?",
        "Check order ORD-002 for me please.",
        "Where is order ORD-003?",
    ]

    print("Running tasks through circuit breaker...\n")

    for i, task in enumerate(tasks, 1):
        print(f"Task {i}: {task}")
        print(f"  Circuit state: {breaker.state.name}")
        try:
            output = await breaker.run(task)
            print(f"  Result: {output}")
        except CircuitBreakerOpen as exc:
            print(f"  [BLOCKED] {exc}")
        except ModelBehaviorError as exc:
            print(f"  [AGENT ERROR] {exc.message}")
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
