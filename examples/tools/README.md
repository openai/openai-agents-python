# Tool examples

Follow the [example setup](../README.md), configure `OPENAI_API_KEY`, and run one script from the repository root:

```sh
uv run python examples/tools/web_search.py
```

Hosted examples make live API calls and may incur charges.
Check [provider support for file search, Code Interpreter, image generation and hosted shell](hosted_tool_compatibility.md).

| Task | Examples | Setup or effects |
| --- | --- | --- |
| Search the web | [Web search](web_search.py), [filters](web_search_filters.py) | Web-search access. |
| Index and search a file | [File search](file_search.py) | Creates an uploaded file and vector store, then attempts cleanup. |
| Run hosted code or generate an image | [Code Interpreter](code_interpreter.py), [image generation](image_generator.py) | Access to the hosted capability. |
| Use hosted shell skills | [Inline skill](container_shell_inline_skill.py), [saved skill](container_shell_skill_reference.py) | Hosted shell access; the saved skill requires your project's `OPENAI_SHELL_SKILL_ID`. |
| Execute local commands | [Shell](shell.py), [approval](shell_human_in_the_loop.py), [local skill](local_shell_skill.py), [local Python](local_code_execution.md) | Commands run with host permissions unless you provide external isolation. |
| Apply a patch | [Patch](apply_patch.py) | Uses a temporary workspace. |
| Drive a browser | [Computer use](computer_use.py) | Playwright and Chromium. |
| Call the separate Codex CLI | [Codex tool](codex.py), [same thread](codex_same_thread.py) | Installed and authenticated CLI plus outer-agent credentials; see the [Codex guide](../codex/README.md). |
| Discover or call tools dynamically | [Tool search](tool_search.py), [programmatic calls](programmatic_tool_calling.py) | Access to the configured model and hosted capabilities. |

For computer use, install Playwright and Chromium, then run the script:

```sh
uv run --with playwright python -m playwright install chromium
uv run --with playwright python examples/tools/computer_use.py
```

The browser uses a local demo page and headless Chromium by default. Set `COMPUTER_USE_HEADLESS=0` for a visible browser or `COMPUTER_USE_START_URL` to change the page.
