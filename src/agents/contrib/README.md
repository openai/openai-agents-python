# Agent-OS Governance for OpenAI Agents SDK

Kernel-level guardrails and policy enforcement for OpenAI Agents SDK using [Agent-OS](https://github.com/imran-siddique/agent-os).

## Features

- **Output Guardrails**: Block dangerous patterns in agent outputs
- **Input Validation**: Filter malicious inputs before processing
- **Tool Control**: Limit which tools agents can use
- **Rate Limiting**: Cap tool invocations per run
- **Violation Handling**: Callbacks for policy violations

## Installation

```bash
pip install openai-agents[governance]
# or
pip install agent-os-kernel
```

## Quick Start

```python
from agents import Agent, Runner
from agents.contrib import create_governance_guardrail

# Create guardrail with simple config
guardrail = create_governance_guardrail(
    blocked_patterns=["DROP TABLE", "rm -rf", "DELETE FROM"],
    blocked_tools=["shell_execute"],
    max_tool_calls=10,
)

# Create agent with guardrail
agent = Agent(
    name="analyst",
    instructions="Analyze data safely",
    output_guardrails=[guardrail],
)

# Run agent
result = await Runner.run(agent, "Analyze Q4 sales data")
```

## Advanced Usage

### Full Policy Configuration

```python
from agents.contrib import GovernanceGuardrail, GovernancePolicy

policy = GovernancePolicy(
    # Content Filtering
    blocked_patterns=["DROP TABLE", "rm -rf", "DELETE FROM"],
    max_output_length=100_000,
    
    # Tool Control
    blocked_tools=["shell_execute", "file_delete"],
    allowed_tools=["search", "calculator", "code_executor"],
    max_tool_calls=20,
    
    # Approval
    require_human_approval=False,
    approval_tools=["database_write"],
)

guardrail = GovernanceGuardrail(policy)
```

### Handling Violations

```python
def on_violation(violation):
    print(f"BLOCKED: {violation.policy_name}")
    print(f"  Reason: {violation.description}")
    # Send alert, log to SIEM, etc.

guardrail = GovernanceGuardrail(policy, on_violation=on_violation)
```

### Using GovernedRunner

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

# Runner handles guardrail injection
result = await runner.run(agent, "Analyze Q4 sales")

# Check violations
print(f"Violations: {len(runner.violations)}")
for v in runner.violations:
    print(f"  - {v.description}")
```

### Input Validation

```python
from agents.contrib import GovernanceGuardrail, GovernancePolicy

policy = GovernancePolicy(
    blocked_patterns=["DROP TABLE", "rm -rf"],
)

guardrail = GovernanceGuardrail(policy)

# Check input before sending to agent
user_input = "Delete the users table"
violation = guardrail.check_input(user_input)

if violation:
    print(f"Input blocked: {violation.description}")
else:
    result = await Runner.run(agent, user_input)
```

## Integration with Agent-OS Kernel

For full kernel-level governance:

```python
from agent_os import KernelSpace
from agent_os.policies import SQLPolicy, CostControlPolicy
from agents import Agent, Runner

# Create kernel with policies
kernel = KernelSpace(policy=[
    SQLPolicy(allow=["SELECT"], deny=["DROP", "DELETE"]),
    CostControlPolicy(max_cost_usd=100),
])

# Wrap agent execution in kernel
@kernel.register
async def run_agent(input_text):
    return await Runner.run(agent, input_text)

# Execute with full governance
result = await kernel.execute(run_agent, "Analyze data")
```

## Links

- [Agent-OS GitHub](https://github.com/imran-siddique/agent-os)
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/)
