import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
from openai.types.responses.response_output_item import ImageGenerationCall, LocalShellCall, McpCall
from pydantic import BaseModel, Field
from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.pretty import Pretty
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.text import Text
from typing_extensions import TypedDict

from agents import ItemHelpers, TResponseInputItem
from agents.items import (
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ReasoningItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
)
from agents.sandbox import Manifest
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session import BaseSandboxClient, SandboxSession
from agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    StreamEvent,
)
from examples.auto_mode import input_with_fallback, is_auto_mode

DEFAULT_SANDBOX_IMAGE = "sandbox-tutorials:latest"
console = Console()
PanelBody = Group | Pretty | Text
PrintableEvent: TypeAlias = StreamEvent | str
SandboxClient: TypeAlias = BaseSandboxClient[Any]
InteractiveTurnRunner: TypeAlias = Callable[
    [list[TResponseInputItem]], Awaitable[list[TResponseInputItem]]
]


class ApplyPatchOperationPayload(TypedDict):
    path: str
    type: Literal["create_file", "update_file", "delete_file"]
    diff: str


class ApplyPatchCallPayload(TypedDict):
    type: Literal["apply_patch_call"]
    call_id: str
    operation: ApplyPatchOperationPayload


class Question(BaseModel):
    query: str = Field(description="User-facing question to ask.")
    options: list[str] = Field(
        default_factory=list,
        description="Suggested answer options. The UI always adds a custom free-text choice.",
    )


class QuestionAnswer(BaseModel):
    question: str = Field(description="The question that was asked.")
    answer: str = Field(description="The user's selected or free-text answer.")


def load_env_defaults(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip('"').strip("'")
        if normalized_key:
            os.environ.setdefault(normalized_key, normalized_value)


async def create_sandbox_client_and_session(
    *,
    manifest: Manifest,
    use_docker: bool,
    image: str = DEFAULT_SANDBOX_IMAGE,
) -> tuple[SandboxClient, SandboxSession]:
    if use_docker:
        try:
            from docker import from_env as docker_from_env  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                "Docker-backed runs require the Docker SDK. Install repo dependencies with `make sync`."
            ) from exc

        client: SandboxClient = DockerSandboxClient(
            docker_from_env(environment=build_docker_environment())
        )
        sandbox = await client.create(
            manifest=manifest,
            options=DockerSandboxClientOptions(image=image),
        )
        return client, sandbox

    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=manifest)
    return client, sandbox


def build_docker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if environment.get("DOCKER_HOST") or environment.get("DOCKER_CONTEXT"):
        return environment

    # Respect whichever Docker context the CLI is currently using, including Docker Desktop
    # and Colima, without taking a direct dependency on a specific daemon provider.
    try:
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            capture_output=True,
            check=True,
            text=True,
        )
        docker_host = json.loads(result.stdout.strip() or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return environment

    if isinstance(docker_host, str) and docker_host:
        environment["DOCKER_HOST"] = docker_host
    return environment


def prompt_with_fallback(prompt: str, fallback: str) -> str:
    if is_auto_mode():
        return input_with_fallback(prompt, fallback).strip()

    try:
        return Prompt.ask(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return fallback


def ask_user_questions(questions: list[Question]) -> list[QuestionAnswer]:
    answers: list[QuestionAnswer] = []

    for question_index, question in enumerate(questions, start=1):
        suggested_options = [option.strip() for option in question.options if option.strip()]
        custom_choice_index = len(suggested_options) + 1
        options_text = Text.from_markup(
            "\n".join(
                [
                    *(
                        f"[cyan]{index}.[/cyan] {option}"
                        for index, option in enumerate(
                            suggested_options,
                            start=1,
                        )
                    ),
                    f"[cyan]{custom_choice_index}.[/cyan] Use your own text",
                ]
            )
        )

        console.print(
            Panel(
                Group(
                    Text(question.query),
                    options_text,
                ),
                title=f"Question {question_index}",
                border_style="magenta",
                box=box.ROUNDED,
                expand=False,
            )
        )

        while True:
            choice = prompt_with_fallback(
                f"[bold cyan]Select[/bold cyan] 1-{custom_choice_index}",
                "1" if suggested_options else str(custom_choice_index),
            )
            if choice.isdigit() and 1 <= int(choice) <= len(suggested_options):
                answer = suggested_options[int(choice) - 1]
                break
            if choice.isdigit() and int(choice) == custom_choice_index:
                answer = prompt_with_fallback(
                    "[bold cyan]Your answer[/bold cyan]",
                    suggested_options[0] if suggested_options else "Use a conservative assumption.",
                )
                if answer:
                    break
                continue
            if choice and not choice.isdigit():
                answer = choice
                break

            console.print(
                f"[red]Please enter a number from 1 to {custom_choice_index}, or custom text.[/red]"
            )

        answers.append(QuestionAnswer(question=question.query, answer=answer))

    console.print(
        Panel(
            Pretty([answer.model_dump(mode="json") for answer in answers], expand_all=True),
            title="Question answers",
            border_style="magenta",
            box=box.ROUNDED,
            expand=False,
        )
    )
    return answers


async def run_interactive_loop(
    *,
    conversation: list[TResponseInputItem],
    no_interactive: bool,
    run_turn: InteractiveTurnRunner,
) -> list[TResponseInputItem]:
    if no_interactive or is_auto_mode():
        return conversation

    console.print("[dim]Enter follow-up prompts. Press Ctrl-D or Ctrl-C to finish.[/dim]")
    while True:
        try:
            next_message = Prompt.ask("[bold cyan]user[/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not next_message:
            continue

        conversation.append({"role": "user", "content": next_message})
        conversation = await run_turn(conversation)

    return conversation


def print_event(event: PrintableEvent) -> None:
    if isinstance(event, str):
        console.print()
        console.rule("[bold green]Final output[/bold green]", style="green")
        console.print(
            Panel(
                Markdown(event or "_No final output returned._"),
                border_style="green",
                box=box.ROUNDED,
                expand=False,
            )
        )
        return

    if isinstance(event, AgentUpdatedStreamEvent):
        console.print(
            Panel(
                Pretty(event.new_agent.name, expand_all=True),
                title="Agent updated",
                border_style="cyan",
                box=box.ROUNDED,
                expand=False,
            )
        )
        return

    if isinstance(event, RawResponsesStreamEvent):
        return

    body: PanelBody
    match event.item:
        case ReasoningItem() as item:
            body = Pretty(item, expand_all=True)
            title = f"Reasoning item: {event.name.replace('_', ' ')}"
        case ToolCallItem() as item:
            tool_name = "tool"
            body = Pretty(item.raw_item, expand_all=True)
            match item.raw_item:
                case ResponseFunctionToolCall() as raw_item:
                    tool_name = raw_item.name
                    payload = json.loads(raw_item.arguments) if raw_item.arguments else {}
                    if tool_name == "exec_command":
                        command = payload["cmd"]
                        if "\\n" in command and "\n" not in command:
                            command = command.replace("\\n", "\n")
                        body = Group(
                            Pretty(
                                {key: value for key, value in payload.items() if key != "cmd"},
                                expand_all=True,
                            ),
                            Syntax(command, "bash", theme="ansi_dark", word_wrap=True),
                        )
                    else:
                        body = Pretty(payload, expand_all=True)
                case ResponseComputerToolCall() as raw_item:
                    tool_name = "computer"
                    body = Pretty(raw_item, expand_all=True)
                case ResponseFileSearchToolCall() as raw_item:
                    tool_name = "file_search"
                    body = Pretty(raw_item, expand_all=True)
                case ResponseFunctionWebSearch() as raw_item:
                    tool_name = "web_search"
                    body = Pretty(raw_item, expand_all=True)
                case McpCall() as raw_item:
                    tool_name = "mcp"
                    body = Pretty(raw_item, expand_all=True)
                case ResponseCodeInterpreterToolCall() as raw_item:
                    tool_name = "code_interpreter"
                    body = Pretty(raw_item, expand_all=True)
                case ImageGenerationCall() as raw_item:
                    tool_name = "image_generation"
                    body = Pretty(raw_item, expand_all=True)
                case LocalShellCall() as raw_item:
                    tool_name = "local_shell"
                    body = Pretty(raw_item, expand_all=True)
                case dict() as raw_item:
                    tool_name = "apply_patch"
                    payload = cast(ApplyPatchCallPayload, raw_item)["operation"]
                    body = Group(
                        Pretty(
                            {
                                "path": payload["path"],
                                "type": payload["type"],
                            },
                            expand_all=True,
                        ),
                        Syntax(payload["diff"], "diff", theme="ansi_dark", word_wrap=True),
                    )
            title = f"Tool call: {tool_name}"
        case ToolCallOutputItem() as item:
            body = Text(item.output) if isinstance(item.output, str) else Pretty(item.output)
            title = "Tool output"
        case MessageOutputItem() as item:
            output = ItemHelpers.text_message_output(item)
            body = Text(output) if isinstance(output, str) else Pretty(output, expand_all=True)
            title = "Message output"
        case ToolSearchCallItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Tool search call"
        case ToolSearchOutputItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Tool search output"
        case HandoffCallItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Handoff call"
        case HandoffOutputItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Handoff output"
        case MCPListToolsItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "MCP list tools"
        case MCPApprovalRequestItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "MCP approval request"
        case MCPApprovalResponseItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "MCP approval response"
        case CompactionItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Compaction"
        case ToolApprovalItem() as item:
            body = Pretty(item.raw_item, expand_all=True)
            title = "Tool approval"

    console.print(
        Panel(
            body,
            title=title,
            border_style="cyan",
            box=box.ROUNDED,
            expand=False,
        )
    )
