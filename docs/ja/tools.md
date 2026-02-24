---
search:
  exclude: true
---
# ツール

ツールにより、エージェントはアクションを実行できます。たとえば、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータの使用などです。この SDK は 5 つのカテゴリーをサポートします。

-   OpenAI がホストするツール: OpenAI サーバー上でモデルと並行して実行されます。
-   ローカルランタイムツール: お使いの環境で実行されます (コンピュータ操作、シェル、パッチ適用)。
-   Function Calling: 任意の Python 関数をツールとしてラップします。
-   Agents as tools: フルの ハンドオフ なしで、エージェントを呼び出し可能なツールとして公開します。
-   実験的: Codex ツール: ツール呼び出しから、ワークスペース範囲の Codex タスクを実行します。

## ツールタイプ選択

このページをカタログとして使い、制御できるランタイムに合うセクションへ移動してください。

| もしあなたが...したいなら | ここから始めてください |
| --- | --- |
| OpenAI 管理ツール (Web 検索、ファイル検索、Code Interpreter、ホストされる MCP、画像生成) を使う | [ホストされるツール](#hosted-tools) |
| 自分のプロセスまたは環境でツールを実行する | [ローカルランタイムツール](#local-runtime-tools) |
| Python 関数をツールとしてラップする | [関数ツール](#function-tools) |
| ハンドオフ なしであるエージェントから別のエージェントを呼び出す | [Agents as tools](#agents-as-tools) |
| エージェントからワークスペース範囲の Codex タスクを実行する | [実験的: Codex ツール](#experimental-codex-tool) |

## ホストされるツール

OpenAI は、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供します。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は、エージェントが Web を検索できるようにします。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は、OpenAI ベクトルストアから情報を取得できるようにします。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は、LLM がサンドボックス化された環境でコードを実行できるようにします。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。

高度なホスト型検索オプション:

-   `FileSearchTool` は、`vector_store_ids` と `max_num_results` に加えて、`filters`、`ranking_options`、`include_search_results` をサポートします。
-   `WebSearchTool` は、`filters`、`user_location`、`search_context_size` をサポートします。

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

### ホストされるコンテナシェル + スキル

`ShellTool` は、OpenAI がホストするコンテナ実行もサポートします。ローカルランタイムではなく、管理されたコンテナ内でモデルにシェルコマンドを実行させたい場合は、このモードを使用してください。

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

後続の実行で既存コンテナを再利用するには、`environment={"type": "container_reference", "container_id": "cntr_..."}` を設定します。

知っておくべきこと:

-   ホストされるシェルは、Responses API のシェルツールを通じて利用できます。
-   `container_auto` はリクエスト向けにコンテナをプロビジョニングします。`container_reference` は既存のものを再利用します。
-   `container_auto` には `file_ids` と `memory_limit` も含められます。
-   `environment.skills` は、スキル参照とインラインのスキルバンドルを受け付けます。
-   ホストされる環境では、`ShellTool` に `executor`、`needs_approval`、`on_approval` を設定しないでください。
-   `network_policy` は `disabled` と `allowlist` のモードをサポートします。
-   allowlist モードでは、`network_policy.domain_secrets` により、名前でドメインスコープのシークレットを注入できます。
-   完全な例は `examples/tools/container_shell_skill_reference.py` と `examples/tools/container_shell_inline_skill.py` を参照してください。
-   OpenAI プラットフォームガイド: [Shell](https://platform.openai.com/docs/guides/tools-shell) および [Skills](https://platform.openai.com/docs/guides/tools-skills)。

## ローカルランタイムツール

ローカルランタイムツールはお使いの環境で実行され、実装を提供する必要があります。

-   [`ComputerTool`][agents.tool.ComputerTool]: GUI/ブラウザ自動化を有効化するには、[`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
-   [`ShellTool`][agents.tool.ShellTool]: ローカル実行とホストされるコンテナ実行の両方に対応した最新のシェルツールです。
-   [`LocalShellTool`][agents.tool.LocalShellTool]: 旧来の local-shell 統合です。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: 差分をローカルで適用するには [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装します。
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

任意の Python 関数をツールとして使用できます。Agents SDK がツールを自動的にセットアップします。

-   ツール名は Python 関数名になります (または名前を指定できます)。
-   ツールの説明は関数の docstring から取得されます (または説明を指定できます)。
-   関数入力のスキーマは、関数の引数から自動的に作成されます。
-   各入力の説明は、無効化されていない限り docstring から取得されます。

関数シグネチャの抽出には Python の `inspect` モジュールを使用し、docstring の解析には [`griffe`](https://mkdocstrings.github.io/griffe/)、スキーマ作成には `pydantic` を使用します。

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

1.  関数の引数には任意の Python 型を使用でき、関数は sync/async いずれでも構いません。
2.  docstrings が存在する場合、説明および引数説明の取得に使用されます。
3.  関数は任意で `context` を受け取れます (第 1 引数である必要があります)。ツール名、説明、使用する docstring スタイルなどのオーバーライドも設定できます。
4.  デコレートした関数をツール一覧へ渡せます。

??? note "出力を表示するには展開"

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

テキスト出力を返すことに加えて、関数ツールの出力として 1 つ以上の画像またはファイルを返せます。そのためには、次のいずれかを返せます。

-   画像: [`ToolOutputImage`][agents.tool.ToolOutputImage] (または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict])
-   ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] (または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict])
-   テキスト: 文字列、または文字列化可能なオブジェクト、もしくは [`ToolOutputText`][agents.tool.ToolOutputText] (または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict])

### カスタム関数ツール

場合によっては、Python 関数をツールとして使いたくないことがあります。その場合は、必要に応じて [`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。必要なのは次のとおりです。

-   `name`
-   `description`
-   `params_json_schema` (引数の JSON スキーマ)
-   `on_invoke_tool` ( [`ToolContext`][agents.tool_context.ToolContext] と、JSON 文字列としての引数を受け取り、ツール出力 (例: テキスト、構造化されたツール出力オブジェクト、または出力のリスト) を返す async 関数)

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

前述のとおり、ツールのスキーマを抽出するために関数シグネチャを自動解析し、ツールおよび個々の引数の説明を抽出するために docstring を解析します。これに関する注意点:

1. シグネチャ解析は `inspect` モジュールで行います。型注釈により引数の型を理解し、全体スキーマを表す Pydantic モデルを動的に構築します。Python のプリミティブ、Pydantic モデル、TypedDict など、ほとんどの型をサポートします。
2. docstring の解析には `griffe` を使用します。サポートされる docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式は自動検出を試みますが、これはベストエフォートであり、`function_tool` 呼び出し時に明示的に設定できます。`use_docstring_info` を `False` に設定することで docstring 解析を無効化することもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

### Pydantic Field による引数の制約と説明

Pydantic の [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) を使用して、ツール引数に制約 (例: 数値の最小/最大、文字列の長さやパターン) と説明を追加できます。Pydantic と同様に、両方の形式をサポートします。デフォルトベース (`arg: int = Field(..., ge=1)`) と `Annotated` (`arg: Annotated[int, Field(..., ge=1)]`) です。生成される JSON スキーマとバリデーションには、これらの制約が含まれます。

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

### 関数ツールのタイムアウト

async の関数ツールについては、`@function_tool(timeout=...)` で呼び出しごとのタイムアウトを設定できます。

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

タイムアウトに達した場合、既定の挙動は `timeout_behavior="error_as_result"` であり、モデルから見えるタイムアウトメッセージ (例: `Tool 'slow_lookup' timed out after 2 seconds.`) を送信します。

タイムアウト処理を制御できます。

-   `timeout_behavior="error_as_result"` (デフォルト): 復旧できるよう、タイムアウトメッセージをモデルへ返します。
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

    タイムアウト設定は async の `@function_tool` ハンドラーでのみサポートされます。

### 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際に、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に、LLM へ返すエラーレスポンスを提供する関数です。

-   デフォルト (何も渡さない場合) では、エラーが発生したことを LLM に伝える `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡した場合はそれが実行され、そのレスポンスが LLM に送信されます。
-   明示的に `None` を渡した場合は、ツール呼び出しエラーは再 raise され、ユーザー側で処理できます。これは、モデルが不正な JSON を生成した場合の `ModelBehaviorError` や、コードがクラッシュした場合の `UserError` などになり得ます。

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

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。

## Agents as tools

ワークフローによっては、制御を ハンドオフ する代わりに、中央のエージェントが専門エージェントのネットワークをオーケストレーションしたい場合があります。これを、エージェントをツールとしてモデル化することで実現できます。

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

`agent.as_tool` 関数は、エージェントをツールに変換しやすくするための便利メソッドです。`max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session`、`needs_approval` といった一般的なランタイムオプションをサポートします。また、`parameters`、`input_builder`、`include_input_schema` による構造化入力もサポートします。高度なオーケストレーション (例: 条件付きリトライ、フォールバック動作、複数エージェント呼び出しの連鎖) では、ツール実装内で `Runner.run` を直接使用してください。

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

### ツールエージェントの構造化入力

既定では、`Agent.as_tool()` は単一の文字列入力 (`{"input": "..."}`) を想定しますが、`parameters` (Pydantic モデルまたは dataclass 型) を渡すことで、構造化スキーマを公開できます。

追加オプション:

- `include_input_schema=True` は、生成されるネストされた入力に完全な JSON Schema を含めます。
- `input_builder=...` は、構造化ツール引数をネストされたエージェント入力に変換する方法を完全にカスタマイズできます。
- `RunContextWrapper.tool_input` は、ネストされた実行コンテキスト内の、解析済みの構造化ペイロードを含みます。

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

`Agent.as_tool(..., needs_approval=...)` は `function_tool` と同じ承認フローを使用します。承認が必要な場合、実行は一時停止し、保留中の項目が `result.interruptions` に表示されます。その後、`result.to_state()` を使用し、`state.approve(...)` または `state.reject(...)` を呼び出してから再開します。完全な一時停止/再開パターンについては [Human-in-the-loop ガイド](human_in_the_loop.md) を参照してください。

### カスタム出力抽出

特定のケースでは、中央のエージェントへ返す前に、ツールエージェントの出力を変更したい場合があります。これは次のような場合に役立ちます。

-   サブエージェントのチャット履歴から特定の情報 (例: JSON ペイロード) を抽出する。
-   エージェントの最終回答を変換または再フォーマットする (例: Markdown をプレーンテキストや CSV に変換する)。
-   出力を検証する、またはエージェントの応答が欠落している/不正な場合にフォールバック値を提供する。

`as_tool` メソッドに `custom_output_extractor` 引数を指定することで実現できます。

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

### ネストされたエージェント実行の ストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ストリームが完了した後に最終出力を返しつつ、ネストされたエージェントが発行する ストリーミング イベントを購読できます。

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

想定されること:

- イベントタイプは `StreamEvent["type"]` を反映します: `raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を提供すると、ネストされたエージェントは自動的に ストリーミング モードで実行され、最終出力を返す前にストリームをドレインします。
- ハンドラーは同期/非同期のどちらでもよく、各イベントは到着順に配信されます。
- `tool_call` は、モデルのツール呼び出し経由でツールが呼び出された場合に存在します。直接呼び出しでは `None` のままになることがあります。
- 完全に実行可能なサンプルは `examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

`is_enabled` パラメーターを使用すると、実行時にエージェントツールを条件付きで有効/無効にできます。これにより、コンテキスト、ユーザー設定、またはランタイム条件に基づいて、LLM が利用できるツールを動的にフィルタリングできます。

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

`is_enabled` パラメーターは次を受け付けます。

-   **Boolean 値**: `True` (常に有効) または `False` (常に無効)
-   **呼び出し可能関数**: `(context, agent)` を受け取り boolean を返す関数
-   **Async 関数**: 複雑な条件ロジックのための async 関数

無効化されたツールは実行時に LLM から完全に隠されるため、次の用途に有用です。

-   ユーザー権限に基づく機能ゲーティング
-   環境別のツール利用可否 (dev vs prod)
-   異なるツール構成の A/B テスト
-   ランタイム状態に基づく動的なツールフィルタリング

## 実験的: Codex ツール

`codex_tool` は Codex CLI をラップし、エージェントがツール呼び出し中にワークスペース範囲のタスク (シェル、ファイル編集、MCP ツール) を実行できるようにします。このサーフェスは実験的であり、変更される可能性があります。
デフォルトでは、ツール名は `codex` です。カスタム名を設定する場合、それは `codex` であるか、`codex_` で始まる必要があります。
エージェントが複数の Codex ツールを含む場合、それぞれが一意の名前を使用する必要があります (Codex 以外のツールも含めて)。

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

-   Auth: `CODEX_API_KEY` (推奨) または `OPENAI_API_KEY` を設定するか、`codex_options={"api_key": "..."}` を渡します。
-   Runtime: `codex_options.base_url` は CLI の base URL をオーバーライドします。
-   Binary resolution: `codex_options.codex_path_override` (または `CODEX_PATH`) を設定して CLI パスを固定します。設定しない場合、SDK は `PATH` から `codex` を解決し、その後、同梱の vendor バイナリへフォールバックします。
-   Environment: `codex_options.env` はサブプロセス環境を完全に制御します。これが提供される場合、サブプロセスは `os.environ` を継承しません。
-   Stream limits: `codex_options.codex_subprocess_stream_limit_bytes` (または `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`) は stdout/stderr リーダーの上限を制御します。有効範囲は `65536` から `67108864`、デフォルトは `8388608` です。
-   Inputs: ツール呼び出しには、`inputs` に少なくとも 1 つのアイテムとして `{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を含める必要があります。
-   Thread defaults: `default_thread_options` で `model_reasoning_effort`、`web_search_mode` (旧来の `web_search_enabled` より推奨)、`approval_policy`、`additional_directories` を設定します。
-   Turn defaults: `default_turn_options` で `idle_timeout_seconds` とキャンセル `signal` を設定します。
-   Safety: `sandbox_mode` を `working_directory` と組み合わせます。Git リポジトリ外では `skip_git_repo_check=True` を設定します。
-   Run-context thread persistence: `use_run_context_thread_id=True` は、同じコンテキストを共有する実行にわたり、実行コンテキスト内に `thread_id` を保存して再利用します。これには変更可能な実行コンテキスト (例: `dict` または書き込み可能なオブジェクトフィールド) が必要です。
-   Run-context key defaults: 保存されるキーのデフォルトは、`name="codex"` の場合は `codex_thread_id`、`name="codex_<suffix>"` の場合は `codex_thread_id_<suffix>` です。オーバーライドするには `run_context_thread_id_key` を設定します。
-   Thread ID precedence: 呼び出しごとの `thread_id` 入力が優先され、次に (有効化されていれば) run-context の `thread_id`、最後に設定済みの `thread_id` オプションが続きます。
-   Streaming: `on_stream` は、thread/turn のライフサイクルイベントとアイテムイベント (`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list`、および `error` アイテム更新) を受け取ります。
-   Outputs: 結果には `response`、`usage`、`thread_id` が含まれます。`usage` は `RunContextWrapper.usage` に追加されます。
-   Structure: `output_schema` は、型付き出力が必要な場合に構造化された Codex 応答を強制します。
-   完全に実行可能なサンプルは `examples/tools/codex.py` と `examples/tools/codex_same_thread.py` を参照してください。