---
search:
  exclude: true
---
# ツール

ツールは、エージェントがデータ取得、コード実行、外部 API 呼び出し、さらにはコンピュータ操作などのアクションを実行できるようにします。SDK は次の 4 つの カテゴリー をサポートします:

- OpenAI がホストするツール: モデルと同じ OpenAI サーバー 上で実行されます。
- ローカル実行ツール: あなたの環境で実行されます（コンピュータ操作、シェル、パッチ適用）。
- Function Calling: 任意の Python 関数をツールとしてラップします。
- ツールとしてのエージェント: フルな ハンドオフ なしで、エージェントを呼び出し可能なツールとして公開します。

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する場合、OpenAI はいくつかの組み込みツールを提供します:

- [`WebSearchTool`][agents.tool.WebSearchTool]: エージェントが Web 検索 を実行できます。
- [`FileSearchTool`][agents.tool.FileSearchTool]: OpenAI ベクトルストア から情報を取得できます。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool]: LLM がサンドボックス環境でコードを実行できます。
- [`HostedMCPTool`][agents.tool.HostedMCPTool]: リモート MCP サーバー のツールをモデルに公開します。
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

## ローカル実行ツール

ローカル実行ツールはあなたの環境で動作し、実装の提供が必要です:

- [`ComputerTool`][agents.tool.ComputerTool]: [`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装して、GUI／ブラウザの自動化を有効にします。
- [`ShellTool`][agents.tool.ShellTool] または [`LocalShellTool`][agents.tool.LocalShellTool]: コマンドを実行するシェル実行器を提供します。
- [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装してローカルに差分を適用します。

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

任意の Python 関数をツールとして使えます。Agents SDK が自動的にツールをセットアップします:

- ツール名は Python 関数名になります（任意で名前を指定可能）
- ツールの説明は関数の docstring から取得されます（任意で説明を指定可能）
- 関数入力のスキーマは関数の引数から自動生成されます
- 各入力の説明は、無効化しない限り関数の docstring から取得されます

Python の `inspect` モジュールを使って関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、スキーマ生成には `pydantic` を使用します。

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

1. 関数の引数には任意の Python 型を使え、関数は同期／非同期のどちらでも構いません。
2. docstring が存在する場合、説明と引数の説明を取得するために使用します。
3. 関数は任意で `context` を受け取れます（最初の引数である必要があります）。ツール名、説明、docstring スタイルなどの上書き設定も可能です。
4. デコレートした関数をツール一覧に渡せます。

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

### 関数ツールから画像やファイルを返す

テキスト出力に加えて、関数ツールの出力として 1 つまたは複数の画像やファイルを返せます。次のいずれかを返してください:

- 画像: [`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- テキスト: 文字列または文字列化可能なオブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

Python 関数をツールとして使いたくない場合もあります。その場合は [`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。次を指定する必要があります:

- `name`
- `description`
- 引数の JSON スキーマ である `params_json_schema`
- [`ToolContext`][agents.tool_context.ToolContext] と引数の JSON 文字列を受け取り、ツールの出力を文字列で返す非同期関数 `on_invoke_tool`

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

前述のとおり、ツールのスキーマを抽出するために関数シグネチャを自動解析し、ツールおよび各引数の説明を抽出するために docstring を解析します。注意点:

1. シグネチャ解析は `inspect` モジュール経由で行います。型アノテーションから引数の型を理解し、全体のスキーマを表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict などほとんどの型をサポートします。
2. `griffe` を使って docstring を解析します。サポートする docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式は自動検出を試みますがベストエフォートのため、`function_tool` 呼び出し時に明示的に設定できます。`use_docstring_info` を `False` に設定して docstring 解析を無効化することもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしてのエージェント

一部のワークフローでは、制御を引き渡す代わりに、中央のエージェントが専門エージェントのネットワークをオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデリングすることで実現できます。

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

### ツール化エージェントのカスタマイズ

`agent.as_tool` 関数は、エージェントを簡単にツール化するための便利メソッドです。ただし、すべての設定をサポートしているわけではありません。例えば `max_turns` は設定できません。高度なユースケースでは、ツール実装内で直接 `Runner.run` を使用してください:

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

場合によっては、中央のエージェントに返す前にツール化したエージェントの出力を変更したいことがあります。例えば次のような場合に有用です:

- サブエージェントのチャット履歴から特定の情報（例: JSON ペイロード）を抽出する。
- エージェントの最終回答を変換・再整形する（例: Markdown をプレーンテキストや CSV に変換）。
- 出力を検証し、応答が欠落または不正な場合にフォールバック値を提供する。

これは、`as_tool` メソッドに `custom_output_extractor` 引数を渡すことで行えます:

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

### ネストしたエージェント実行の ストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ストリーム完了後に最終出力を返しつつ、ネストしたエージェントが発行する ストリーミング イベントを受け取れます。

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

想定される動作:

- イベント種別は `StreamEvent["type"]` に対応します: `raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を指定すると、ネストしたエージェントは自動的に ストリーミング モードで実行され、最終出力を返す前にストリームが排出されます。
- ハンドラーは同期／非同期のいずれでも構いません。各イベントは到着順に配信されます。
- ツールがモデルのツール呼び出し経由で起動された場合は `tool_call_id` が存在します。直接呼び出しでは `None` の場合があります。
- 完全な実行可能サンプルは `examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

実行時に `is_enabled` パラメーター を使って、エージェントのツールを条件付きで有効化または無効化できます。これにより、コンテキスト、ユーザー の設定、実行時条件に基づいて、LLM に提供するツールを動的にフィルタリングできます。

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

- **ブール値**: `True`（常に有効）または `False`（常に無効）
- **呼び出し可能関数**: `(context, agent)` を受け取り、真偽値を返す関数
- **非同期関数**: 複雑な条件ロジック向けの async 関数

無効化されたツールは実行時に LLM から完全に隠れるため、次の用途に有用です:

- ユーザー 権限に基づく機能ゲーティング
- 環境別のツール可用性（dev と prod）
- 異なるツール構成の A/B テスト
- 実行時状態に基づく動的ツールフィルタリング

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へエラー応答を提供する関数です。

- 既定（何も渡さない場合）では、エラーが発生したことを LLM に知らせる `default_tool_error_function` を実行します。
- 独自のエラー関数を渡した場合は、それが代わりに実行され、その応答が LLM に送信されます。
- 明示的に `None` を渡すと、ツール呼び出しのエラーは再スローされ、あなたが処理することになります。これは、モデルが不正な JSON を生成した場合の `ModelBehaviorError`、あなたのコードがクラッシュした場合の `UserError` などになり得ます。

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