# Run the basic examples with Codex

These examples use the stock Codex CLI. Start with the [Codex provider setup](../../docs/models/codex.md), then run commands from the repository root. Omitting `--codex` keeps the default provider and its original service requirements.

```sh
# Keep two questions in one local SDK session.
uv run -m examples.basic.previous_response_id --local --codex --stream

# Read the introduction from the bundled PDF.
uv run -m examples.basic.local_file --codex

# Return a local image from an SDK tool.
uv run -m examples.basic.image_tool_output --codex --file examples/basic/media/image_bison.jpg
```

The session example does not create a hosted response ID. The PDF examples require Poppler's `pdftotext` on `PATH`: install `poppler-utils` on Debian or Ubuntu, or `poppler` with Homebrew. These examples extract text; they do not perform OCR or interpret page layout.

With `--codex`, [remote_pdf.py](remote_pdf.py) and [remote_image.py](remote_image.py) download their fixed sample URLs in the app. Add `--file PATH` to use a local PDF or JPEG instead. The provider does not fetch arbitrary URLs supplied by the model.

For computation and data, see [local code execution](../tools/local_code_execution.md) and [querying a SQLite database](../sandbox/extensions/daytona/usaspending_text2sql/README.md).

## Use a local Ollama model

Both modes use your local Ollama server at `http://localhost:11434/v1`. [Install and start Ollama](https://cookbook.openai.com/articles/gpt-oss/run-locally-ollama), then download the model:

```sh
ollama pull gpt-oss:20b
ollama list
```

Once `ollama list` shows `gpt-oss:20b`, run the example from the repository root:

```sh
uv run -m examples.basic.hello_world_gpt_oss
uv run -m examples.basic.hello_world_gpt_oss --codex
```

The first command calls Chat Completions. The second runs Codex and needs an Ollama version that serves `/v1/responses`. If the model is missing, both commands fail. Neither falls back to a cloud model.
