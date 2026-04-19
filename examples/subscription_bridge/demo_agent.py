from __future__ import annotations

import argparse
import asyncio
import threading
import time
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled

try:
    from .server import make_server
except ImportError:  # pragma: no cover - script execution path
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from examples.subscription_bridge.server import make_server

Backend = Literal["codex", "claude"]


def default_model_for_backend(backend: Backend) -> str:
    if backend == "claude":
        return "claude/claude-sonnet-4-6"
    return "codex/gpt-5.4"


def resolve_model(backend: Backend, model: str | None) -> str:
    return model or default_model_for_backend(backend)


def normalize_api_base_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped
    return f"{stripped}/v1"


@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny and 72 F."


async def run_demo(*, prompt: str, backend: Backend, model: str | None, api_base_url: str) -> str:
    set_tracing_disabled(disabled=True)
    client = AsyncOpenAI(base_url=normalize_api_base_url(api_base_url), api_key="dummy")
    agent = Agent(
        name="Subscription Bridge Demo",
        instructions="Use tools when useful, then answer clearly.",
        model=OpenAIChatCompletionsModel(
            model=resolve_model(backend, model),
            openai_client=client,
        ),
        tools=[get_weather],
    )
    result = await Runner.run(agent, prompt)
    return str(result.final_output)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local openai-agents-python demo through the subscription bridge."
    )
    parser.add_argument(
        "--backend",
        choices=["codex", "claude"],
        default="codex",
        help="Which CLI-backed bridge backend to use.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional full model name override, e.g. codex/gpt-5.4 or claude/claude-sonnet-4-6.",
    )
    parser.add_argument(
        "--prompt",
        default="What is the weather in Tokyo?",
        help="Prompt to send to the demo agent.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bridge host for local embedded mode.")
    parser.add_argument(
        "--port", type=int, default=8787, help="Bridge port for local embedded mode."
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Use an already-running bridge instead of starting a local embedded bridge.",
    )
    parser.add_argument(
        "--workdir",
        default=str(Path.cwd()),
        help="Working directory to pass to the local embedded bridge.",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> str:
    if args.base_url:
        return await run_demo(
            prompt=args.prompt,
            backend=args.backend,
            model=args.model,
            api_base_url=args.base_url,
        )

    httpd = make_server(
        args.host,
        args.port,
        default_backend=args.backend,
        workdir=Path(args.workdir).resolve(),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        return await run_demo(
            prompt=args.prompt,
            backend=args.backend,
            model=args.model,
            api_base_url=f"http://{args.host}:{args.port}",
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    print(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
