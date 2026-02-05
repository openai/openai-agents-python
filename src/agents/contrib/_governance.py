# Copyright (c) Agent-OS Contributors. All rights reserved.
# Licensed under the MIT License.
"""Kernel-level governance for OpenAI Agents SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union


class ViolationType(Enum):
    """Types of policy violations."""

    TOOL_BLOCKED = "tool_blocked"
    TOOL_LIMIT_EXCEEDED = "tool_limit_exceeded"
    CONTENT_FILTERED = "content_filtered"
    OUTPUT_BLOCKED = "output_blocked"
    INPUT_BLOCKED = "input_blocked"
    TIMEOUT = "timeout"


@dataclass
class PolicyViolation:
    """Represents a policy violation event."""

    violation_type: ViolationType
    policy_name: str
    description: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GovernancePolicy:
    """Policy configuration for agent governance.

    Attributes:
        blocked_patterns: Regex patterns to block in inputs/outputs.
        blocked_tools: Tool names that cannot be used.
        allowed_tools: If set, only these tools can be used.
        max_tool_calls: Maximum tool invocations per run.
        max_output_length: Maximum output length in characters.
        require_human_approval: Require approval for certain actions.
        approval_tools: Tools requiring human approval.
    """

    blocked_patterns: List[str] = field(default_factory=list)
    blocked_tools: List[str] = field(default_factory=list)
    allowed_tools: Optional[List[str]] = None
    max_tool_calls: int = 50
    max_output_length: int = 100_000
    require_human_approval: bool = False
    approval_tools: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Compile regex patterns."""
        self._compiled_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.blocked_patterns
        ]


class GovernanceGuardrail:
    """Guardrail that enforces Agent-OS governance policies.

    Implements the OpenAI Agents SDK guardrail interface to provide
    kernel-level policy enforcement.

    Example:
        ```python
        from agents import Agent, Runner
        from agents.contrib import GovernanceGuardrail, GovernancePolicy

        policy = GovernancePolicy(
            blocked_patterns=["DROP TABLE", "rm -rf"],
            blocked_tools=["shell_execute"],
            max_tool_calls=10,
        )

        guardrail = GovernanceGuardrail(policy)

        agent = Agent(
            name="analyst",
            instructions="Analyze data safely",
            output_guardrails=[guardrail],
        )
        ```
    """

    def __init__(
        self,
        policy: GovernancePolicy,
        on_violation: Optional[Callable[[PolicyViolation], None]] = None,
    ):
        """Initialize governance guardrail.

        Args:
            policy: Governance policy to enforce.
            on_violation: Callback when violations occur.
        """
        self.policy = policy
        self.on_violation = on_violation
        self._tool_calls = 0
        self._violations: List[PolicyViolation] = []

    async def run(self, context: Any, agent: Any, output: Any) -> Any:
        """Execute guardrail check on agent output.

        Args:
            context: Run context.
            agent: Agent that produced output.
            output: Output to check.

        Returns:
            GuardrailFunctionOutput with tripwire if violation detected.
        """
        # Import here to avoid circular dependency
        try:
            from agents.guardrail import GuardrailFunctionOutput
        except ImportError:
            # Fallback for testing
            @dataclass
            class GuardrailFunctionOutput:
                output_info: Any = None
                tripwire_triggered: bool = False

        output_str = str(output) if output else ""

        # Check patterns
        for pattern in self.policy._compiled_patterns:
            if pattern.search(output_str):
                violation = self._record_violation(
                    ViolationType.OUTPUT_BLOCKED,
                    f"Output contains blocked pattern: {pattern.pattern}",
                    pattern=pattern.pattern,
                )
                return GuardrailFunctionOutput(
                    output_info=f"BLOCKED: {violation.description}",
                    tripwire_triggered=True,
                )

        # Check length
        if len(output_str) > self.policy.max_output_length:
            violation = self._record_violation(
                ViolationType.OUTPUT_BLOCKED,
                f"Output exceeds max length ({len(output_str)} > {self.policy.max_output_length})",
            )
            return GuardrailFunctionOutput(
                output_info=f"BLOCKED: {violation.description}",
                tripwire_triggered=True,
            )

        return GuardrailFunctionOutput(tripwire_triggered=False)

    def check_tool(self, tool_name: str) -> Optional[PolicyViolation]:
        """Check if a tool is allowed by policy.

        Args:
            tool_name: Name of tool to check.

        Returns:
            PolicyViolation if blocked, None if allowed.
        """
        # Check blocked list
        if tool_name in self.policy.blocked_tools:
            return self._record_violation(
                ViolationType.TOOL_BLOCKED,
                f"Tool '{tool_name}' is blocked by policy",
                tool_name=tool_name,
            )

        # Check allowed list
        if (
            self.policy.allowed_tools is not None
            and tool_name not in self.policy.allowed_tools
        ):
            return self._record_violation(
                ViolationType.TOOL_BLOCKED,
                f"Tool '{tool_name}' not in allowed list",
                tool_name=tool_name,
            )

        # Check limit
        self._tool_calls += 1
        if self._tool_calls > self.policy.max_tool_calls:
            return self._record_violation(
                ViolationType.TOOL_LIMIT_EXCEEDED,
                f"Tool call limit exceeded ({self._tool_calls} > {self.policy.max_tool_calls})",
            )

        return None

    def check_input(self, input_text: str) -> Optional[PolicyViolation]:
        """Check input text for policy violations.

        Args:
            input_text: Input to check.

        Returns:
            PolicyViolation if blocked, None if allowed.
        """
        for pattern in self.policy._compiled_patterns:
            if pattern.search(input_text):
                return self._record_violation(
                    ViolationType.INPUT_BLOCKED,
                    f"Input contains blocked pattern: {pattern.pattern}",
                    pattern=pattern.pattern,
                )
        return None

    def _record_violation(
        self,
        violation_type: ViolationType,
        description: str,
        **details: Any,
    ) -> PolicyViolation:
        """Record a policy violation."""
        violation = PolicyViolation(
            violation_type=violation_type,
            policy_name=violation_type.value,
            description=description,
            details=details,
        )
        self._violations.append(violation)

        if self.on_violation:
            self.on_violation(violation)

        return violation

    @property
    def violations(self) -> List[PolicyViolation]:
        """Get all violations."""
        return self._violations.copy()

    def reset(self):
        """Reset guardrail state for new run."""
        self._tool_calls = 0
        self._violations = []


class GovernedRunner:
    """Runner wrapper with governance enforcement.

    Wraps the standard Runner to enforce policies on all operations.

    Example:
        ```python
        from agents import Agent
        from agents.contrib import GovernedRunner, GovernancePolicy

        policy = GovernancePolicy(
            blocked_patterns=["DROP TABLE"],
            max_tool_calls=10,
        )

        runner = GovernedRunner(policy)

        agent = Agent(
            name="analyst",
            instructions="Analyze data",
        )

        result = await runner.run(agent, "Analyze Q4 sales")
        print(f"Violations: {len(runner.violations)}")
        ```
    """

    def __init__(
        self,
        policy: GovernancePolicy,
        on_violation: Optional[Callable[[PolicyViolation], None]] = None,
    ):
        """Initialize governed runner.

        Args:
            policy: Governance policy to enforce.
            on_violation: Callback when violations occur.
        """
        self.policy = policy
        self.guardrail = GovernanceGuardrail(policy, on_violation)

    async def run(
        self,
        agent: Any,
        input_text: str,
        **kwargs: Any,
    ) -> Any:
        """Run agent with governance.

        Args:
            agent: Agent to run.
            input_text: Input text.
            **kwargs: Additional arguments passed to Runner.run.

        Returns:
            Agent result.

        Raises:
            ValueError: If input violates policy.
        """
        # Import here to avoid circular dependency
        try:
            from agents import Runner
        except ImportError:
            raise ImportError("OpenAI Agents SDK not installed")

        # Check input
        violation = self.guardrail.check_input(input_text)
        if violation:
            raise ValueError(f"Input blocked: {violation.description}")

        # Reset guardrail state
        self.guardrail.reset()

        # Add guardrail to agent if not present
        if hasattr(agent, "output_guardrails"):
            if self.guardrail not in agent.output_guardrails:
                agent.output_guardrails = list(agent.output_guardrails or [])
                agent.output_guardrails.append(self.guardrail)

        # Run agent
        result = await Runner.run(agent, input_text, **kwargs)

        return result

    @property
    def violations(self) -> List[PolicyViolation]:
        """Get all violations."""
        return self.guardrail.violations


def create_governance_guardrail(
    blocked_patterns: Optional[List[str]] = None,
    blocked_tools: Optional[List[str]] = None,
    max_tool_calls: int = 50,
    on_violation: Optional[Callable[[PolicyViolation], None]] = None,
) -> GovernanceGuardrail:
    """Factory function to create a governance guardrail.

    Convenience function for common use cases.

    Args:
        blocked_patterns: Regex patterns to block.
        blocked_tools: Tool names to block.
        max_tool_calls: Maximum tool invocations.
        on_violation: Callback for violations.

    Returns:
        Configured GovernanceGuardrail.

    Example:
        ```python
        from agents import Agent
        from agents.contrib import create_governance_guardrail

        guardrail = create_governance_guardrail(
            blocked_patterns=["DROP TABLE", "rm -rf"],
            blocked_tools=["shell"],
            max_tool_calls=10,
        )

        agent = Agent(
            name="analyst",
            output_guardrails=[guardrail],
        )
        ```
    """
    policy = GovernancePolicy(
        blocked_patterns=blocked_patterns or [],
        blocked_tools=blocked_tools or [],
        max_tool_calls=max_tool_calls,
    )
    return GovernanceGuardrail(policy, on_violation)
