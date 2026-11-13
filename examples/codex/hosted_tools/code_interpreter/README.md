# Calculate in a provider container

Complete the [hosted-tool setup](../README.md) first: install the matching preview wheel, activate its Python environment, and run from the source archive's root. Supply `OPENAI_API_KEY` through your environment. These commands make live OpenAI requests and can incur charges.

## Run the calculation

This run path requires an existing provider container. Create one using the provider's [container API](https://developers.openai.com/api/reference/resources/containers/methods/create), then supply its ID. For a run without an existing container, use the [automatic-container declaration](#let-the-provider-select-a-container) below:

```sh
export OPENAI_CONTAINER_ID=cntr_your_existing_container
python -m examples.codex.hosted_tools.code_interpreter.main
```

The script asks the provider to calculate squares and create a CSV. It prints the code, output, and container-file citations. Look for `Container:` and `Artifact:` lines. Each artifact line includes its container ID, file ID, and filename. Download needed artifacts with the provider's [container-file content API](https://developers.openai.com/api/reference/resources/containers/subresources/files/subresources/content/methods/retrieve) while the container remains active. A container file ID is not an ordinary Files API download ID.

## Let the provider select a container

To let OpenAI create or reuse a container, import `CodeInterpreter` from `agents` and replace the declaration in `main.py` with:

```python
CodeInterpreter(
    container={
        "type": "auto",
        "memory_limit": "1g",
        "network_policy": {"type": "disabled"},
    }
)
```

You can also supply uploaded `file_ids` in that auto configuration. OpenAI owns code execution and auto-container reuse and expiry. Your application owns explicit containers and durable copies of artifacts. Closing the SDK Session does not delete the container. Restoring session history cannot revive an expired container or its Python memory.

This example selects `gpt-5.2` with OpenAI Responses. Other models and providers may not support the tool or its container options. See the shared [setup and ownership notes](../README.md) and the provider's [Code Interpreter guide](https://developers.openai.com/api/docs/guides/tools-code-interpreter).
