---
name: Bug report
about: Report a bug
title: ''
labels: ''
assignees: ''

---

### Please read this first

- **Have you read the docs?** [Agents SDK docs](https://openai.github.io/openai-agents-python/)
- **Have you searched for related issues?** Others may have faced similar issues.

### Describe the bug
<!-- Clearly and concisely describe the bug. -->

### Debug information
- Agents SDK version:
- Related library versions (optional, e.g. `any-llm`, `litellm`, or `pydantic`):
- Python version:
- Operating system:
- Model and model provider:
- Does the issue reproduce with the latest Agents SDK release?
- Does the issue occur consistently or intermittently?

If an error occurred, include the full traceback and any relevant logs. Remove API keys, tokens, model input or output, and other sensitive information before posting.

<!-- Paste the traceback or relevant logs below. -->

```text

```

### Repro steps

Ideally provide a minimal, self-contained Python script that can be run to reproduce the bug.

```python
from agents import Agent, Runner

agent = Agent(
    name="Example agent",
    instructions="...",
    # Add the model and any other settings needed to reproduce the bug.
)

result = Runner.run_sync(agent, "...")
print(result.final_output)
```

### Expected behavior
<!-- Clearly and concisely describe what you expected to happen. -->
