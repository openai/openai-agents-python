---
search:
  exclude: true
---
# ツール

ツールは エージェント にアクションを実行させます。たとえば、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータの使用などです。Agent SDK には次の 3 種類のツールがあります。

-   ホスト型ツール: これらは AI モデルと同じ LLM サーバー上で動作します。OpenAI は retrieval、Web 検索、コンピュータ操作 をホスト型ツールとして提供しています。
-   Function calling: 任意の Python 関数をツールとして使用できます。
-   エージェントをツールとして使用: エージェントをツールとして利用でき、ハンドオフ せずに他の エージェント を呼び出せます。

## ホスト型ツール

OpenAI は、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際にいくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は エージェント に Web を検索させます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI の ベクトルストア から情報を取得できます。
-   [`ComputerTool`][agents.tool.ComputerTool] は コンピュータ操作 の自動化を可能にします。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は LLM がサンドボックス環境でコードを実行できるようにします。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] はリモートの MCP サーバー のツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] はプロンプトから画像を生成します。
-   [`LocalShellTool`][agents.tool.LocalShellTool] はローカルマシン上でシェルコマンドを実行します。

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

## 関数ツール

任意の Python 関数をツールとして使用できます。Agents SDK がツールの設定を自動で行います。

-   ツール名は Python 関数名になります（任意で名前を指定可能）
-   ツールの説明は関数の docstring から取得します（任意で説明を指定可能）
-   関数入力のスキーマは、関数の引数から自動生成されます
-   各入力の説明は、無効化しない限り関数の docstring から取得します

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

1.  関数の引数には任意の Python 型を使用でき、関数は同期/非同期どちらでも構いません。
2.  docstring があれば、ツールと引数の説明の取得に使用します。
3.  関数は任意で `context` を受け取れます（最初の引数である必要があります）。ツール名や説明、docstring のスタイルなどのオーバーライドも設定できます。
4.  デコレートした関数をツールのリストに渡せます。

??? note "Expand to see output"

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

### 関数ツールからの画像やファイルの返却

テキスト出力に加えて、関数ツールの出力として 1 つまたは複数の画像やファイルを返すこともできます。次のいずれかを返せます。

-   画像: [`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   テキスト: 文字列または文字列化可能オブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

Python 関数をツールとして使いたくない場合もあります。必要であれば直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。以下を提供する必要があります。

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON スキーマ）
-   `on_invoke_tool`（[`ToolContext`][agents.tool_context.ToolContext] と引数の JSON 文字列を受け取り、ツールの出力を文字列で返す非同期関数）

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

前述のとおり、ツールのスキーマを抽出するために関数シグネチャを自動解析し、ツール本体と各引数の説明を抽出するために docstring を解析します。ポイントは次のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。型アノテーションから引数の型を把握し、全体スキーマを表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict など、ほとんどの型をサポートします。
2. docstring の解析には `griffe` を使用します。サポートする docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式は自動検出を試みますがベストエフォートのため、`function_tool` 呼び出し時に明示的に指定できます。`use_docstring_info` を `False` に設定すると docstring 解析を無効化できます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして使用

一部のワークフローでは、ハンドオフ せずに中央の エージェント が専門特化した エージェント 群をオーケストレーションしたい場合があります。これは エージェント をツールとしてモデリングすることで実現できます。

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

### ツール化したエージェントのカスタマイズ

`agent.as_tool` 関数は エージェント をツールに変換しやすくするための簡便メソッドです。ただし、すべての設定をサポートしているわけではありません。例えば `max_turns` は設定できません。高度なユースケースでは、ツール実装内で直接 `Runner.run` を使用してください。

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

状況によっては、中央の エージェント に返す前にツール化した エージェント の出力を加工したいことがあります。例えば次のような場合に有用です。

-   サブエージェントのチャット履歴から特定情報（例: JSON ペイロード）を抽出する。
-   エージェント の最終回答を変換・再整形する（例: Markdown をプレーンテキストや CSV に変換）。
-   出力を検証し、 エージェント の応答が欠落または不正な場合にフォールバック値を用意する。

これは `as_tool` メソッドに `custom_output_extractor` 引数を指定することで実現できます。

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

### 条件付きツール有効化

実行時に `is_enabled` パラメーター を使って エージェント ツールを条件付きで有効化/無効化できます。これにより、コンテキスト、ユーザー の嗜好、実行時条件に基づいて、LLM が利用可能なツールを動的にフィルタリングできます。

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

`is_enabled` パラメーター は次を受け付けます。

-   **Boolean values**: `True`（常に有効）または `False`（常に無効）
-   **Callable functions**: `(context, agent)` を受け取り boolean を返す関数
-   **Async functions**: 複雑な条件ロジック向けの非同期関数

無効化されたツールは実行時に LLM から完全に隠されるため、次の用途に役立ちます。

-   ユーザー 権限に基づく機能ゲーティング
-   環境別のツール可用性（dev と prod）
-   ツール構成の A/B テスト
-   実行時状態に基づく動的ツールフィルタリング

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM にエラーレスポンスを提供する関数です。

-   既定（何も渡さない場合）は、エラーが発生したことを LLM に伝える `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡した場合はそれが実行され、そのレスポンスが LLM に送信されます。
-   明示的に `None` を渡した場合、ツール呼び出しエラーは再スローされ、呼び出し側で処理することになります。これは、モデルが不正な JSON を生成した場合の `ModelBehaviorError`、あなたのコードがクラッシュした場合の `UserError` などになり得ます。

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