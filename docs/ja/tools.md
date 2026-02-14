---
search:
  exclude: true
---
# ツール

ツールにより、エージェントはアクションを実行できます。たとえば、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作まで可能です。 SDK は 5 つのカテゴリーをサポートします。

-   OpenAI がホストするツール: OpenAI サーバー上でモデルと並行して実行されます。
-   ローカルランタイムツール: お使いの環境で実行されます（ computer use 、 shell 、 apply patch ）。
-   Function Calling: 任意の Python 関数をツールとしてラップします。
-   Agents as tools: 完全なハンドオフなしで、エージェントを呼び出し可能なツールとして公開します。
-   実験的: Codex ツール: ツール呼び出しからワークスペーススコープの Codex タスクを実行します。

## ホスト型ツール

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供します。

-   [`WebSearchTool`][agents.tool.WebSearchTool] により、エージェントは Web 検索できます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] により、 OpenAI Vector Stores から情報を取得できます。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] により、 LLM はサンドボックス化された環境でコードを実行できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] により、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。

ホスト型検索の高度なオプション:

-   `FileSearchTool` は、 `vector_store_ids` と `max_num_results` に加えて、 `filters` 、 `ranking_options` 、 `include_search_results` をサポートします。
-   `WebSearchTool` は、 `filters` 、 `user_location` 、 `search_context_size` をサポートします。

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)

async def main():
    result = await Runner.run(agent, "Which coffee shop should I go to, taking into account my preferences and the weather today in SF?")
    print(result.final_output)
```

### ホスト型コンテナシェル + スキル

`ShellTool` は OpenAI がホストするコンテナでの実行もサポートします。ローカルランタイムではなく、管理されたコンテナ内でモデルにシェルコマンドを実行させたい場合はこのモードを使用してください。

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.2",
    instructions="Use the mounted skill when helpful.",
    tools=[
        ShellTool(
            environment={
                "type": "container_auto",
                "network_policy": {"type": "disabled"},
                "skills": [csv_skill],
            }
        )
    ],
)

result = await Runner.run(
    agent,
    "Use the configured skill to analyze CSV files in /mnt/data and summarize totals by region.",
)
print(result.final_output)
```

後続の実行で既存のコンテナを再利用するには、 `environment={"type": "container_reference", "container_id": "cntr_..."}` を設定します。

知っておくべきこと:

-   ホスト型シェルは、 Responses API のシェルツール経由で利用できます。
-   `container_auto` はリクエストのためにコンテナをプロビジョニングし、 `container_reference` は既存のコンテナを再利用します。
-   `container_auto` は `file_ids` と `memory_limit` も含められます。
-   `environment.skills` は、スキル参照とインラインのスキルバンドルを受け付けます。
-   ホスト型環境では、 `ShellTool` に `executor` 、 `needs_approval` 、 `on_approval` を設定しないでください。
-   `network_policy` は `disabled` と `allowlist` のモードをサポートします。
-   allowlist モードでは、 `network_policy.domain_secrets` が名前でドメインスコープのシークレットを注入できます。
-   完全な例は `examples/tools/container_shell_skill_reference.py` と `examples/tools/container_shell_inline_skill.py` を参照してください。
-   OpenAI プラットフォームガイド: [Shell](https://platform.openai.com/docs/guides/tools-shell) および [Skills](https://platform.openai.com/docs/guides/tools-skills)。

## ローカルランタイムツール

ローカルランタイムツールはお使いの環境で実行され、実装を提供する必要があります。

-   [`ComputerTool`][agents.tool.ComputerTool]: GUI / ブラウザ自動化を有効にするため、 [`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
-   [`ShellTool`][agents.tool.ShellTool]: ローカル実行とホスト型コンテナ実行の両方に対応した最新のシェルツールです。
-   [`LocalShellTool`][agents.tool.LocalShellTool]: レガシーのローカルシェル統合です。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装して、差分をローカルに適用します。
-   ローカルシェルスキルは `ShellTool(environment={"type": "local", "skills": [...]})` で利用できます。

```python
from agents import Agent, ApplyPatchTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)
    async def screenshot(self): return ""
    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def update_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def delete_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        # ComputerTool expects a Computer/AsyncComputer implementation; omitted here for brevity.
    ],
)
```

## 関数ツール

任意の Python 関数をツールとして使用できます。 Agents SDK がツールを自動的にセットアップします。

-   ツール名は Python 関数名になります（または名前を指定できます）
-   ツールの説明は関数の docstring から取得されます（または説明を指定できます）
-   関数入力のスキーマは、関数の引数から自動的に作成されます
-   各入力の説明は、無効化しない限り、 docstring から取得されます

Python の `inspect` モジュールで関数シグネチャを抽出し、 [`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、 `pydantic` でスキーマを作成します。

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper, function_tool


class Location(TypedDict):
    lat: float
    long: float

@function_tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@function_tool(name_override="fetch_data")  # (3)!
def read_file(ctx: RunContextWrapper[Any], path: str, directory: str | None = None) -> str:
    """Read the contents of a file.

    Args:
        path: The path to the file to read.
        directory: The directory to read the file from.
    """
    # In real life, we'd read the file from the file system
    return "<file contents>"


agent = Agent(
    name="Assistant",
    tools=[fetch_weather, read_file],  # (4)!
)

for tool in agent.tools:
    if isinstance(tool, FunctionTool):
        print(tool.name)
        print(tool.description)
        print(json.dumps(tool.params_json_schema, indent=2))
        print()

```

1.  関数の引数には任意の Python 型を使用でき、関数は sync / async のどちらでも構いません。
2.  docstring がある場合、説明と引数の説明の取得に使用されます。
3.  関数は任意で `context` を受け取れます（先頭の引数である必要があります）。また、ツール名、説明、使用する docstring スタイルなどの override も設定できます。
4.  デコレートした関数をツールのリストに渡せます。

??? note "出力を表示するには展開してください"

    ```
    fetch_weather
    Fetch the weather for a given location.
    {
    "$defs": {
      "Location": {
        "properties": {
          "lat": {
            "title": "Lat",
            "type": "number"
          },
          "long": {
            "title": "Long",
            "type": "number"
          }
        },
        "required": [
          "lat",
          "long"
        ],
        "title": "Location",
        "type": "object"
      }
    },
    "properties": {
      "location": {
        "$ref": "#/$defs/Location",
        "description": "The location to fetch the weather for."
      }
    },
    "required": [
      "location"
    ],
    "title": "fetch_weather_args",
    "type": "object"
    }

    fetch_data
    Read the contents of a file.
    {
    "properties": {
      "path": {
        "description": "The path to the file to read.",
        "title": "Path",
        "type": "string"
      },
      "directory": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "The directory to read the file from.",
        "title": "Directory"
      }
    },
    "required": [
      "path"
    ],
    "title": "fetch_data_args",
    "type": "object"
    }
    ```

### 関数ツールから画像またはファイルを返す

テキスト出力を返すことに加えて、関数ツールの出力として 1 つまたは複数の画像やファイルを返せます。そのためには、次のいずれかを返します。

-   画像: [`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   テキスト: 文字列または文字列化可能なオブジェクト、あるいは [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

場合によっては、 Python 関数をツールとして使いたくないことがあります。その場合は、好みに応じて [`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。提供が必要なものは次のとおりです。

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON schema ）
-   `on_invoke_tool`（ [`ToolContext`][agents.tool_context.ToolContext] と、 JSON 文字列として渡される引数を受け取り、ツール出力（例: テキスト、 structured tool output オブジェクト、または出力のリスト）を返す async 関数）

```python
from typing import Any

from pydantic import BaseModel

from agents import RunContextWrapper, FunctionTool



def do_some_work(data: str) -> str:
    return "done"


class FunctionArgs(BaseModel):
    username: str
    age: int


async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
    parsed = FunctionArgs.model_validate_json(args)
    return do_some_work(data=f"{parsed.username} is {parsed.age} years old")


tool = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)
```

### 引数と docstring の自動解析

前述のとおり、ツール用スキーマを抽出するために関数シグネチャを自動的に解析し、ツールおよび個別引数の説明を抽出するために docstring を解析します。これに関する注意点は次のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。引数の型を理解するために型アノテーションを使用し、全体スキーマを表す Pydantic モデルを動的に構築します。 Python のプリミティブ、 Pydantic モデル、 TypedDict など、ほとんどの型をサポートします。
2. docstring の解析には `griffe` を使用します。サポートされる docstring 形式は `google` 、 `sphinx` 、 `numpy` です。 docstring 形式は自動検出を試みますが、これはベストエフォートであり、 `function_tool` の呼び出し時に明示的に設定できます。また、 `use_docstring_info` を `False` に設定して docstring 解析を無効化することもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

### Pydantic Field による引数の制約と説明

Pydantic の [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) を使用して、ツール引数に制約（例: 数値の min/max 、文字列の長さやパターン）や説明を追加できます。 Pydantic と同様に、 default ベース（ `arg: int = Field(..., ge=1)` ）と `Annotated`（ `arg: Annotated[int, Field(..., ge=1)]` ）の両形式がサポートされます。生成される JSON schema とバリデーションには、これらの制約が含まれます。

```python
from typing import Annotated
from pydantic import Field
from agents import function_tool

# Default-based form
@function_tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@function_tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

## Agents as tools

いくつかのワークフローでは、制御をハンドオフする代わりに、中央のエージェントが専門エージェントのネットワークをオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデル化することで実現できます。

```python
from agents import Agent, Runner
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You translate the user's message to Spanish",
)

french_agent = Agent(
    name="French agent",
    instructions="You translate the user's message to French",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate."
        "If asked for multiple translations, you call the relevant tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

async def main():
    result = await Runner.run(orchestrator_agent, input="Say 'Hello, how are you?' in Spanish.")
    print(result.final_output)
```

### ツールエージェントのカスタマイズ

`agent.as_tool` 関数は、エージェントをツールに変換しやすくするための便利メソッドです。 `max_turns` 、 `run_config` 、 `hooks` 、 `previous_response_id` 、 `conversation_id` 、 `session` 、 `needs_approval` などの一般的なランタイムオプションをサポートします。また、 `parameters` 、 `input_builder` 、 `include_input_schema` による構造化入力もサポートします。高度なオーケストレーション（例: 条件付きリトライ、フォールバック動作、複数のエージェント呼び出しのチェーン）には、ツール実装内で `Runner.run` を直接使用してください。

```python
@function_tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

### ツールエージェント向けの構造化入力

デフォルトでは、 `Agent.as_tool()` は単一の文字列入力（ `{"input": "..."}` ）を期待しますが、 `parameters`（ Pydantic モデルまたは dataclass 型）を渡すことで構造化スキーマを公開できます。

追加オプション:

- `include_input_schema=True` は、生成されるネストされた入力に完全な JSON Schema を含めます。
- `input_builder=...` は、構造化されたツール引数がネストされたエージェント入力になる方法を完全にカスタマイズできます。
- `RunContextWrapper.tool_input` には、ネストされた実行コンテキスト内で解析済みの構造化ペイロードが含まれます。

```python
from pydantic import BaseModel, Field


class TranslationInput(BaseModel):
    text: str = Field(description="Text to translate.")
    source: str = Field(description="Source language.")
    target: str = Field(description="Target language.")


translator_tool = translator_agent.as_tool(
    tool_name="translate_text",
    tool_description="Translate text between languages.",
    parameters=TranslationInput,
    include_input_schema=True,
)
```

完全に実行可能な例は `examples/agent_patterns/agents_as_tools_structured.py` を参照してください。

### ツールエージェントの承認ゲート

`Agent.as_tool(..., needs_approval=...)` は `function_tool` と同じ承認フローを使用します。承認が必要な場合、実行は一時停止し、保留中の項目が `result.interruptions` に現れます。その後、 `result.to_state()` を使用し、 `state.approve(...)` または `state.reject(...)` を呼び出してから再開します。一時停止 / 再開の完全なパターンは [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### カスタム出力抽出

場合によっては、中央のエージェントへ返す前にツールエージェントの出力を変更したいことがあります。これは、次のような場合に有用です。

-   サブエージェントのチャット履歴から特定の情報（例: JSON ペイロード）を抽出する。
-   エージェントの最終回答を変換または再整形する（例: Markdown をプレーンテキストや CSV に変換する）。
-   出力を検証する、またはエージェントの応答が欠落している / 不正な形式である場合にフォールバック値を提供する。

これは、 `as_tool` メソッドに `custom_output_extractor` 引数を指定することで行えます。

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # Scan the agent’s outputs in reverse order until we find a JSON-like message from a tool call.
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # Fallback to an empty JSON object if nothing was found
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

### ネストされたエージェント実行のストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ストリーム完了後に最終出力を返しつつ、ネストされたエージェントが発行する ストリーミング イベントを購読できます。

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent'].name} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

期待されること:

- イベントタイプは `StreamEvent["type"]` を反映します: `raw_response_event` 、 `run_item_stream_event` 、 `agent_updated_stream_event`。
- `on_stream` を指定すると、ネストされたエージェントは自動的にストリーミング モードで実行され、最終出力を返す前にストリームが消費されます。
- ハンドラは同期 / 非同期のどちらでもよく、各イベントは到着順に配信されます。
- ツールがモデルのツール呼び出し経由で呼び出される場合は `tool_call` が存在します。直接呼び出しでは `None` のままの場合があります。
- 完全に実行可能なサンプルは `examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

`is_enabled` パラメーターを使用して、実行時にエージェントツールを条件付きで有効化または無効化できます。これにより、コンテキスト、ユーザーの好み、または実行時条件に基づいて、 LLM が利用できるツールを動的にフィルタリングできます。

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = RunContextWrapper(LanguageContext(language_preference="french_spanish"))
    result = await Runner.run(orchestrator, "How are you?", context=context.context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` パラメーターが受け付けるもの:

-   **Boolean 値**: `True`（常に有効）または `False`（常に無効）
-   **呼び出し可能関数**: `(context, agent)` を受け取り boolean を返す関数
-   **Async 関数**: 複雑な条件ロジック向けの async 関数

無効化されたツールは実行時に LLM から完全に隠されるため、次の用途に有用です。

-   ユーザー権限に基づく機能ゲート
-   環境別のツール可用性（ dev vs prod ）
-   異なるツール構成の A/B テスト
-   実行時状態に基づく動的なツールフィルタリング

## 実験的: Codex ツール

`codex_tool` は Codex CLI をラップし、ツール呼び出し中にエージェントがワークスペーススコープのタスク（ shell 、ファイル編集、 MCP ツール）を実行できるようにします。このインターフェースは実験的であり、変更される可能性があります。デフォルトではツール名は `codex` です。カスタム名を設定する場合、それは `codex` であるか、 `codex_` で始まる必要があります。エージェントに複数の Codex ツールを含める場合、各ツールは一意の名前を使用する必要があります（ Codex ツール / 非 Codex ツールを含む）。

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, TurnOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.2-codex",
                model_reasoning_effort="low",
                network_access_enabled=True,
                web_search_mode="disabled",
                approval_policy="never",
            ),
            default_turn_options=TurnOptions(
                idle_timeout_seconds=60,
            ),
            persist_session=True,
        )
    ],
)
```

知っておくべきこと:

-   認証: `CODEX_API_KEY`（推奨）または `OPENAI_API_KEY` を設定するか、 `codex_options={"api_key": "..."}` を渡します。
-   ランタイム: `codex_options.base_url` は CLI の base URL を上書きします。
-   バイナリ解決: `codex_options.codex_path_override`（または `CODEX_PATH`）を設定して CLI パスを固定します。そうでない場合、 SDK はまず `PATH` から `codex` を解決し、次に同梱の vendor バイナリへフォールバックします。
-   環境: `codex_options.env` がサブプロセス環境を完全に制御します。これが指定されると、サブプロセスは `os.environ` を継承しません。
-   ストリーム制限: `codex_options.codex_subprocess_stream_limit_bytes`（または `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）が stdout/stderr リーダーの制限を制御します。有効範囲は `65536` から `67108864` で、デフォルトは `8388608` です。
-   入力: ツール呼び出しには、 `{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を持つアイテムを `inputs` に少なくとも 1 つ含める必要があります。
-   スレッドのデフォルト: `default_thread_options` で `model_reasoning_effort` 、 `web_search_mode`（レガシーの `web_search_enabled` より推奨）、 `approval_policy` 、 `additional_directories` を設定します。
-   ターンのデフォルト: `default_turn_options` で `idle_timeout_seconds` とキャンセル `signal` を設定します。
-   安全性: `sandbox_mode` を `working_directory` と組み合わせます。 Git リポジトリ外では `skip_git_repo_check=True` を設定します。
-   実行コンテキストでのスレッド永続化: `use_run_context_thread_id=True` は、同じコンテキストを共有する複数実行にわたり、 run context に `thread_id` を保存して再利用します。これにはミュータブルな run context（例: `dict` または書き込み可能なオブジェクトフィールド）が必要です。
-   run context キーのデフォルト: 保存されるキーは、 `name="codex"` の場合は `codex_thread_id`、 `name="codex_<suffix>"` の場合は `codex_thread_id_<suffix>` がデフォルトです。上書きするには `run_context_thread_id_key` を設定します。
-   スレッド ID の優先順位: 呼び出しごとの `thread_id` 入力が最優先で、次に（有効化されていれば） run-context の `thread_id`、最後に設定された `thread_id` オプションが続きます。
-   ストリーミング: `on_stream` はスレッド / ターンのライフサイクルイベントと、アイテムイベント（ `reasoning` 、 `command_execution` 、 `mcp_tool_call` 、 `file_change` 、 `web_search` 、 `todo_list` 、 `error` のアイテム更新）を受け取ります。
-   出力: 結果には `response` 、 `usage` 、 `thread_id` が含まれます。 usage は `RunContextWrapper.usage` に追加されます。
-   構造: `output_schema` は、型付き出力が必要な場合に構造化された Codex 応答を強制します。
-   完全に実行可能なサンプルは `examples/tools/codex.py` と `examples/tools/codex_same_thread.py` を参照してください。

## 関数ツールのタイムアウト

`@function_tool(timeout=...)` を使用して、 async 関数ツールに呼び出しごとのタイムアウトを設定できます。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

タイムアウトに達した場合、デフォルト動作は `timeout_behavior="error_as_result"` で、モデルから見えるタイムアウトメッセージ（例: `Tool 'slow_lookup' timed out after 2 seconds.`）を送信します。

タイムアウト処理を制御できます。

-   `timeout_behavior="error_as_result"`（デフォルト）: タイムアウトメッセージをモデルに返して回復できるようにします。
-   `timeout_behavior="raise_exception"`: [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] を raise して実行を失敗させます。
-   `timeout_error_function=...`: `error_as_result` 使用時のタイムアウトメッセージをカスタマイズします。

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError, function_tool


@function_tool(timeout=1.5, timeout_behavior="raise_exception")
async def slow_tool() -> str:
    await asyncio.sleep(5)
    return "done"


agent = Agent(name="Timeout hard-fail", tools=[slow_tool])

try:
    await Runner.run(agent, "Run the tool")
except ToolTimeoutError as e:
    print(f"{e.tool_name} timed out in {e.timeout_seconds} seconds")
```

!!! note

    タイムアウト設定は async の `@function_tool` ハンドラでのみサポートされます。

## 関数ツールにおけるエラー処理

`@function_tool` で関数ツールを作成する際、 `failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へ返すエラー応答を提供する関数です。

-   デフォルト（つまり、何も渡さない場合）では、エラーが発生したことを LLM に伝える `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡した場合は、それが代わりに実行され、応答が LLM に送られます。
-   明示的に `None` を渡した場合、ツール呼び出しエラーは再 raise され、ユーザー側で処理できます。これは、モデルが無効な JSON を生成した場合の `ModelBehaviorError` や、コードがクラッシュした場合の `UserError` などになりえます。

```python
from agents import function_tool, RunContextWrapper
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@function_tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

`FunctionTool` オブジェクトを手動で作成する場合は、 `on_invoke_tool` 関数内でエラーを処理する必要があります。