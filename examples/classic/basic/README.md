# Basic Classic examples

These scripts introduce one Classic API feature at a time. Complete [example setup](../README.md#setup), then run a module from the repository root:

```sh
python -m examples.classic.basic.hello_world
```

| Task | Scripts |
| --- | --- |
| Run an agent | [`hello_world.py`](hello_world.py), [`hello_world_gpt_5.py`](hello_world_gpt_5.py) |
| Use a local GPT-OSS model through Ollama | [`hello_world_gpt_oss.py`](hello_world_gpt_oss.py) |
| Declare tools and inspect tool output | [`tools.py`](tools.py), [`image_tool_output.py`](image_tool_output.py) |
| Stream text, items, or function arguments | [`stream_text.py`](stream_text.py), [`stream_items.py`](stream_items.py), [`stream_function_call_args.py`](stream_function_call_args.py) |
| Stream over a Responses WebSocket | [`stream_ws.py`](stream_ws.py) |
| Supply image or PDF input | [`local_image.py`](local_image.py), [`remote_image.py`](remote_image.py), [`local_file.py`](local_file.py), [`remote_pdf.py`](remote_pdf.py) |
| Configure prompts and structured output | [`dynamic_system_prompt.py`](dynamic_system_prompt.py), [`prompt_template.py`](prompt_template.py), [`non_strict_output_type.py`](non_strict_output_type.py) |
| Inspect lifecycle hooks and usage | [`agent_lifecycle_example.py`](agent_lifecycle_example.py), [`lifecycle_example.py`](lifecycle_example.py), [`usage_tracking.py`](usage_tracking.py) |
| Gate tool inputs and outputs | [`tool_guardrails.py`](tool_guardrails.py) |
| Continue a server-stored response | [`previous_response_id.py`](previous_response_id.py) |
| Configure retries | [`retry.py`](retry.py), [`retry_litellm.py`](retry_litellm.py) |
| Export redacted trace snapshots without model requests | [`trace_redaction.py`](trace_redaction.py) |

The notebook `hello_world_jupyter.ipynb` uses the same Classic imports. Select a kernel from the SDK environment before running its cells.

## Example-specific setup

- `hello_world_gpt_oss.py` connects to Ollama at `http://localhost:11434/v1` with model `gpt-oss:20b`. The script's comments link the model setup guides.
- `prompt_template.py` refers to a project-owned saved prompt with a `poem_style` variable and version `1`. Its embedded prompt ID does not grant access in your project. After creating your own prompt, use `python -m examples.classic.basic.prompt_template --prompt-id your-prompt-id`; add `--dynamic` for the callback variant.
- `retry_litellm.py` uses the `classic-litellm` extra. Its model configuration requires access to the provider named in the script.
- `stream_ws.py` reads `OPENAI_MODEL`, `OPENAI_BASE_URL`, and `OPENAI_WEBSOCKET_BASE_URL` for optional overrides. The default model is `gpt-5.6-sol`.
- The local image and PDF examples read the bundled `media/` files relative to the script. The remote examples submit public URLs to the model API.
