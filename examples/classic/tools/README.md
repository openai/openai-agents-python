# Classic tool examples

These scripts demonstrate hosted tools, application-owned tool execution, and the experimental Codex CLI wrapper. Complete [Classic example setup](../README.md#setup), configure `OPENAI_API_KEY`, and run a module from the repository root:

```sh
python -m examples.classic.tools.web_search
```

| Task | Examples | Additional setup or effects |
| --- | --- | --- |
| Search the web | [`web_search.py`](web_search.py), [`web_search_filters.py`](web_search_filters.py) | Web-search access. |
| Index and search a file | [`file_search.py`](file_search.py) | The script creates a temporary uploaded file and vector store, then attempts cleanup. |
| Run hosted code | [`code_interpreter.py`](code_interpreter.py) | Hosted Code Interpreter access. |
| Generate an image | [`image_generator.py`](image_generator.py) | Image-generation access. |
| Run hosted shell commands with an inline skill | [`container_shell_inline_skill.py`](container_shell_inline_skill.py) | Hosted shell access; bundled `skills/csv-workbench/` files. |
| Run hosted shell commands with a saved skill | [`container_shell_skill_reference.py`](container_shell_skill_reference.py) | Hosted shell access and a skill owned by your project. Set `OPENAI_SHELL_SKILL_ID` and, optionally, `OPENAI_SHELL_SKILL_VERSION`; the embedded default ID does not grant project access. |
| Execute local shell commands | [`shell.py`](shell.py), [`shell_human_in_the_loop.py`](shell_human_in_the_loop.py), [`local_shell_skill.py`](local_shell_skill.py) | Trusted local execution or external isolation. The approval variant requests a decision before execution. |
| Apply a patch | [`apply_patch.py`](apply_patch.py) | A temporary workspace created by the script. |
| Drive a browser | [`computer_use.py`](computer_use.py) | Playwright and Chromium, as shown below. |
| Call the standalone Codex CLI | [`codex.py`](codex.py), [`codex_same_thread.py`](codex_same_thread.py) | A separately installed and configured Codex CLI; see [Classic Codex integration](../../../CLASSIC.md#experimental-codex-integration). |
| Search and call tools dynamically | [`tool_search.py`](tool_search.py), [`programmatic_tool_calling.py`](programmatic_tool_calling.py) | Access to the configured model and hosted capabilities. |

## Browser setup

The computer-use example imports Playwright, which the Classic extra does not install. From an activated SDK environment:

```sh
python -m pip install playwright
python -m playwright install chromium
python -m examples.classic.tools.computer_use
```

By default, the example uses a local demo page and a headless Chromium browser. `COMPUTER_USE_HEADLESS=0` selects a visible browser. `COMPUTER_USE_START_URL` overrides the starting page.

The hosted examples make paid API calls. The local shell examples run with host process permissions; a tool declaration alone does not provide OS isolation.
