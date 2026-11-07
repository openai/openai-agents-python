# Calculate in a provider container

Create a container through the provider API, then supply its ID:

```sh
export OPENAI_CONTAINER_ID=cntr_your_existing_container
python -m examples.codex.hosted_tools.code_interpreter.main
```

The script asks the provider to calculate squares and create a CSV. It prints
the code, output, and container-file citations. Download needed artifacts with
the provider's container-file content API while the container remains active.
A container file ID is not an ordinary Files API download ID.

To let OpenAI create or reuse a container, replace the declaration with:

```python
CodeInterpreter(
    container={
        "type": "auto",
        "memory_limit": "1g",
        "network_policy": {"type": "disabled"},
    }
)
```

You can also supply uploaded `file_ids` in that auto configuration. OpenAI owns
code execution and auto-container reuse and expiry. Your application owns
explicit containers and durable copies of artifacts. Closing the SDK Session
does not delete the container. Restoring session history cannot revive an
expired container or its Python memory.

This example selects `gpt-5.2` with OpenAI Responses. Other models and providers
may not support the tool or its container options. See the shared
[setup and ownership notes](../README.md) and the provider's
[Code Interpreter guide](https://developers.openai.com/api/docs/guides/tools-code-interpreter).
