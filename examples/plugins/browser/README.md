# App-approved browser reads

Use the [v0 plugin preview wheel](../README.md#install-the-preview) in the same environment as this package.

One SDK function, `browser_read(action)`, for a set of browser tasks chosen by the app. In the [offline example](src/agents_browser/example.py), a scripted model has been choosing `hours`. Chromium has been opening a collapsed FAQ panel and reading the newly visible paragraph. No API keys, model requests or website access.

## Local run

Prerequisites — Python 3.10 or later, a local `openai-agents` build with `agents.plugins`, and a host with Chromium sandbox support. From this directory, in the same environment as the SDK

```sh
python -m pip install '.[playwright]'
python -m playwright install chromium
python -m agents_browser.example
```

Expected tool output

```json
{"action": "hours", "text": "Weekdays from 9 to 5."}
```

With Google Chrome already installed, an alternative to the Chromium download and the last command

```sh
python -c 'import asyncio; from agents_browser.example import main; asyncio.run(main("chrome"))'
```

## Your own browser tasks

Registration of a single action, with an app-supplied async function `hours()` returning text

```python
from agents import Agent
from agents_browser import browser

agent = Agent("Desk", plugins=[browser({"hours": hours})])
```

- **Model input** — One action name from the list in the tool description. No URL, selector, script or button from the model. For an unknown name, `Tool call refused.` before dispatch.
- **Model output** — JSON text with the action name and up to 2,000 characters from the page. An extra ellipsis for a longer answer. Page text as untrusted data, never as instructions.
- **Access** — A copy of the action map at factory time. A fresh plugin per user and only names and answers safe for disclosure. For changing permissions, a fresh access check inside each function. Full app permissions for custom functions. Playwright optional.
- **Browser lifetime** — In the [Playwright adapter](src/agents_browser/panels.py), one offline context per read, with JavaScript and downloads disabled. Only trusted app HTML with a collapsed `<details data-testid="…">`, a `<summary>` and one `<p>`. No live navigation or untrusted HTML. The app has been closing the browser after each run, including cancellation.
