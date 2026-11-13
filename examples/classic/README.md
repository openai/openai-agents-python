# Classic examples

These examples use the retained Python runtime through `agents.classic`. The [native examples](../codex/README.md) use the root `agents` API.

## Setup

The [Classic setup guide](../../CLASSIC.md) covers installation of the matching SDK wheel with its `classic` extra. Additional integrations use `classic-*` extras and the dependencies listed in each example's guide. Keep the installed SDK and the example source on the same version.

Commands below use an activated SDK environment from the repository root. Most examples make live requests with `OPENAI_API_KEY` and incur API charges:

```sh
export OPENAI_API_KEY="your-project-api-key"
python -m examples.classic.basic.hello_world
```

The script prints a short haiku about recursion. The wording can vary.

Commands beginning with `uv run` in the individual guides use the repository's development environment. That environment installs optional development dependencies and can build the SDK from source. Follow [contributor setup](../../CONTRIBUTING.md) before using those commands. For an already installed wheel environment, use its `python` and install the example's additional dependencies there; `uv run` selects the project environment instead.

## Choose an example

| Task | Examples | Additional setup |
| --- | --- | --- |
| First run, tools, streaming, images, and files | [Basic examples](basic/README.md) | Per-example model or prompt access. |
| Routing, handoffs, guardrails, and approvals | [Agent patterns](agent_patterns/README.md) | OpenAI API credentials. |
| Customer support routing | [Customer service](customer_service/main.py) | Interactive terminal. |
| Classify requests and gate tools | [Decisions recipes](decisions/README.md) | The documented OpenAI client version. |
| Filter conversation history during handoffs | [Handoff filtering](handoffs/message_filter.py) and [streaming](handoffs/message_filter_streaming.py) | OpenAI API credentials. |
| Connect to remote MCP through Responses | [Hosted MCP first run](hosted_mcp/simple.py) and [connector example](hosted_mcp/connectors.py) | Remote server access; connector credentials in `connectors.py`. |
| Connect to local and remote MCP servers | [MCP examples](mcp/README.md) | Server-specific Node.js, `uvx`, or HTTP setup. |
| Preserve conversation history | [Memory examples](memory/README.md) | Store-specific extras, drivers, and services. |
| Use another model provider | [Model providers](model_providers/README.md) | Adapter extras and provider credentials. |
| Read model reasoning summaries | [Runner reasoning summaries](reasoning_content/runner_example.py) and [provider variants](reasoning_content/) | A compatible model; the GPT-OSS example uses `OPENROUTER_API_KEY`. |
| Generate a report from web research | [Research bot](research_bot/README.md) | `rich` and web-search access. |
| Add financial specialists and report verification | [Financial research](financial_research_agent/README.md) | `rich` and web-search access. |
| Run commands in a managed workspace | [Sandbox examples](sandbox/README.md) | Backend-specific setup and isolation. |
| Use hosted tools or a local computer | [Tool examples](tools/README.md) | Tool-specific setup. |
| Transcribe speech, run an agent, and synthesize audio | [Static voice](voice/static/README.md) or [streamed voice](voice/streamed/README.md) | Voice extra, microphone, audio output, and PortAudio. |
| Talk to a Realtime agent | [Browser app](realtime/app/README.md), [Twilio Media Streams](realtime/twilio/README.md), or [Twilio SIP](realtime/twilio_sip/README.md) | Realtime access and example-specific server dependencies. |
| Connect microphone audio directly to Realtime | [Realtime CLI](realtime/cli/demo.py) | `classic-voice`, `sounddevice`, PortAudio, and audio devices. |
| Delegate speech requests to an order specialist | [Live app](live/app/README.md) | GPT Live access and the documented requirements file. |

The handoff-filter examples preserve the complete history with the default GPT-5 model. To exercise history removal, select a supported non-GPT-5 model with `OPENAI_DEFAULT_MODEL`; the scripts deliberately skip filtering for GPT-5.

## Automated example runs

The [example runner](run_examples.py) collects script entry points and writes per-example logs. Inspect its options before selecting examples:

```sh
python -m examples.classic.run_examples --help
```

`EXAMPLES_INTERACTIVE_MODE=auto` supplies scripted inputs and automatic approvals in participating examples. It does not make model requests free, prevent tool side effects, or provide isolation. Use individual examples first to check their credentials and effects.
