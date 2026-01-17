---
search:
  exclude: true
---
# ツール

ツールは エージェント がアクションを実行できるようにします。データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作 などです。SDK は次の 5 つの カテゴリー をサポートします:

- Hosted OpenAI tools: OpenAI の サーバー 上でモデルと並行して実行されます。
- ローカルランタイムツール: あなたの環境で実行されます（コンピュータ操作、シェル、パッチ適用）。
- Function calling: 任意の Python 関数をツールとしてラップします。
- ツールとしての エージェント: フルな ハンドオフ なしで、エージェント を呼び出し可能なツールとして公開します。
- 実験的: Codex ツール: ツール呼び出しからワークスペース単位の Codex タスクを実行します。

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する場合、OpenAI はいくつかの組み込みツールを提供します:

- [`WebSearchTool`][agents.tool.WebSearchTool]: エージェント が Web 検索 を行えるようにします。
- [`FileSearchTool`][agents.tool.FileSearchTool]: OpenAI の ベクトルストア から情報を取得できます。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool]: LLM がサンドボックス環境でコードを実行できます。
- [`HostedMCPTool`][agents.tool.HostedMCPTool]: リモートの MCP サーバー のツールをモデルに公開します。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool]: プロンプトから画像を生成します。

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

## ローカルランタイムツール

ローカルランタイムツールはあなたの環境で実行され、実装の提供が必要です:

- [`ComputerTool`][agents.tool.ComputerTool]: GUI/ブラウザ自動化を有効にするために [`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
- [`ShellTool`][agents.tool.ShellTool] または [`LocalShellTool`][agents.tool.LocalShellTool]: コマンド実行用のシェル実行器を提供します。
- [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: ローカルで差分を適用するために [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装します。

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

任意の Python 関数をツールとして使用できます。Agents SDK がツールを自動でセットアップします:

- ツール名は Python 関数名になります（名前を指定することも可能）
- ツールの説明は関数の docstring から取得されます（説明を指定することも可能）
- 関数入力のスキーマは、関数の引数から自動的に作成されます
- 各入力の説明は、無効化しない限り関数の docstring から取得されます

Python の `inspect` モジュールで関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析、スキーマ作成には `pydantic` を使用します。

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

1. 関数の引数には任意の Python 型を使用でき、関数は同期でも非同期でも構いません。
2. docstring がある場合、説明と引数の説明を取得するために使用します。
3. 関数は任意で `context` を受け取れます（最初の引数である必要があります）。ツール名、説明、docstring スタイルなどのオーバーライドも設定できます。
4. デコレートした関数をツールのリストに渡せます。

??? note "出力を表示"

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

### 関数ツールから画像やファイルを返す

テキスト出力に加えて、関数ツールの出力として 1 つまたは複数の画像やファイルを返せます。次のいずれかを返してください:

- 画像: [`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- テキスト: 文字列または文字列化可能なオブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

Python 関数をツールとして使いたくない場合もあります。必要に応じて直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。次を提供してください:

- `name`
- `description`
- `params_json_schema`（引数の JSON スキーマ）
- `on_invoke_tool`（[`ToolContext`][agents.tool_context.ToolContext] と JSON 文字列の引数を受け取り、ツール出力を文字列で返す非同期関数）

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

前述のとおり、ツールのスキーマ抽出のために関数シグネチャを自動解析し、ツールや個々の引数の説明抽出のために docstring を解析します。注意点:

1. シグネチャの解析は `inspect` モジュール経由で行います。型アノテーションを用いて引数の型を理解し、全体のスキーマを表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict など、ほとんどの型をサポートします。
2. `griffe` を使用して docstring を解析します。対応する docstring フォーマットは `google`、`sphinx`、`numpy` です。docstring の形式は自動検出を試みますが、ベストエフォートのため `function_tool` 呼び出し時に明示的に設定できます。`use_docstring_info` を `False` に設定して docstring 解析を無効化することもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしての エージェント

一部のワークフローでは、制御を引き渡す代わりに、中央の エージェント が専門特化した エージェント 群をオーケストレーションしたい場合があります。エージェント をツールとしてモデル化することでこれを実現できます。

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

### ツール化した エージェント のカスタマイズ

`agent.as_tool` 関数は、エージェント をツールに変換しやすくするための便利メソッドです。ただし、すべての設定をサポートするわけではありません。例えば、`max_turns` は設定できません。高度なユースケースでは、ツール実装内で直接 `Runner.run` を使用してください:

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

### カスタム出力抽出

場合によっては、中央の エージェント に返す前に ツール化した エージェント の出力を変更したいことがあります。これは次のような場合に有用です:

- サブエージェント のチャット履歴から特定の情報（例: JSON ペイロード）を抽出する。
- エージェント の最終回答を変換または再フォーマットする（例: Markdown をプレーンテキストや CSV に変換）。
- エージェント の応答が欠落または不正な場合に、出力を検証したりフォールバック値を提供する。

`as_tool` メソッドに `custom_output_extractor` 引数を渡すことで実現できます:

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

### ネストした エージェント 実行の ストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ストリーム完了後に最終出力を返しつつ、ネストした エージェント が発行する ストリーミング イベントを受け取れます。

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent']['name']} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

想定されること:

- イベントタイプは `StreamEvent["type"]` を反映します: `raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を提供すると、ネストした エージェント は自動的に ストリーミング モードで実行され、最終出力を返す前にストリームがドレインされます。
- ハンドラーは同期・非同期いずれでも構いません。各イベントは到着順に配信されます。
- ツールがモデルのツール呼び出し経由で起動された場合は `tool_call_id` が含まれます。直接呼び出しでは `None` のことがあります。
- 完全な実行可能サンプルは `examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

実行時に `is_enabled` パラメーター を使用して、エージェント のツールを条件付きで有効化/無効化できます。これにより、コンテキスト、ユーザー の好み、実行時条件に基づいて、LLM に提供するツールを動的にフィルタリングできます。

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

`is_enabled` パラメーター は次を受け付けます:

- **Boolean 値**: `True`（常に有効）または `False`（常に無効）
- **Callable 関数**: `(context, agent)` を受け取り boolean を返す関数
- **Async 関数**: 複雑な条件ロジック向けの非同期関数

無効化されたツールは実行時に LLM から完全に隠されます。用途の例:

- ユーザー 権限に基づく機能ゲーティング
- 環境別のツール提供（開発 vs 本番）
- ツール構成の A/B テスト
- 実行時状態に基づく動的ツールフィルタリング

## 実験的: Codex ツール

`codex_tool` は Codex CLI をラップし、ツール呼び出し中に ワークスペース 単位のタスク（シェル、ファイル編集、MCP ツール）を エージェント が実行できるようにします。このインターフェースは実験的であり、変更される可能性があります。

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.2-codex",
                network_access_enabled=True,
                web_search_enabled=False,
            ),
            persist_session=True,
        )
    ],
)
```

知っておくべきこと:

- 認証: `CODEX_API_KEY`（推奨）または `OPENAI_API_KEY` を設定するか、`codex_options={"api_key": "..."}` を渡します。
- 入力: ツール呼び出しは `inputs` に少なくとも 1 つ、`{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を含める必要があります。
- セーフティ: `sandbox_mode` と `working_directory` を組み合わせて使用します。Git リポジトリ外では `skip_git_repo_check=True` を設定します。
- 動作: `persist_session=True` は単一の Codex スレッドを再利用し、その `thread_id` を返します。
- ストリーミング: `on_stream` は Codex のイベント（reasoning、コマンド実行、MCP ツール呼び出し、ファイル変更、Web 検索）を受け取ります。
- 出力: 結果には `response`、`usage`、`thread_id` が含まれます。usage は `RunContextWrapper.usage` に加算されます。
- 構造: 型付き出力が必要な場合、`output_schema` が構造化された Codex 応答を強制します。
- 完全な実行可能サンプルは `examples/tools/codex.py` を参照してください。

## 関数ツールのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へエラー応答を提供する関数です。

- 既定（何も渡さない場合）では、エラー発生を LLM に伝える `default_tool_error_function` が実行されます。
- 独自のエラー関数を渡した場合はそれが実行され、その応答が LLM に送信されます。
- 明示的に `None` を渡した場合、ツール呼び出しエラーは再送出され、あなたが処理する必要があります。モデルが不正な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などになり得ます。

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

手動で `FunctionTool` オブジェクトを作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。