---
search:
  exclude: true
---
# ツール

ツールは エージェント にアクションを実行させます。たとえばデータの取得、コードの実行、外部 API の呼び出し、さらにコンピュータの利用などです。 Agents SDK には 3 種類のツールクラスがあります。

-   ホスト済みツール: これらは AI モデルと同じ LLM サーバー上で実行されます。 OpenAI はリトリーバル、 Web 検索、コンピュータ操作をホスト済みツールとして提供しています。
-   Function calling: 任意の Python 関数をツールとして使用できます。
-   エージェントをツールとして利用: エージェント をツールとして扱い、ハンドオフせずに他の エージェント を呼び出せます。

## ホスト済みツール

 OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は エージェント に Web 検索 を実行させます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI Vector Stores から情報を取得します。
-   [`ComputerTool`][agents.tool.ComputerTool] は コンピュータ操作 タスクを自動化します。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は LLM がサンドボックス環境でコードを実行できるようにします。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] はリモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] はプロンプトから画像を生成します。
-   [`LocalShellTool`][agents.tool.LocalShellTool] はローカルマシンでシェルコマンドを実行します。

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

## Function tools

任意の Python 関数をツールとして使用できます。 Agents SDK が自動的に設定を行います。

-   ツール名は Python 関数名になります (または任意で指定可能)。
-   ツールの説明は関数の docstring から取得されます (または任意で指定可能)。
-   関数入力のスキーマは関数の引数から自動生成されます。
-   各入力の説明は、無効化しない限り docstring から取得されます。

Python の `inspect` モジュールで関数シグネチャを抽出し、 docstring の解析には [`griffe`](https://mkdocstrings.github.io/griffe/)、スキーマ生成には `pydantic` を使用しています。

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

1.  関数の引数には任意の Python 型を使用でき、同期関数・非同期関数のどちらでも構いません。
2.  docstring が存在する場合、説明および引数の説明を取得します。
3.  関数はオプションで `context` (最初の引数) を受け取れます。またツール名や説明、 docstring スタイルなどのオーバーライドも可能です。
4.  デコレートした関数をツールのリストに渡すだけで利用できます。

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

### カスタム function ツール

Python 関数をツールとして使わない場合は、直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。以下を指定してください。

-   `name`
-   `description`
-   `params_json_schema` ― 引数の JSON スキーマ
-   `on_invoke_tool` ― [`ToolContext`][agents.tool_context.ToolContext] と引数 (JSON 文字列) を受け取り、ツール出力を文字列で返す非同期関数

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

前述のとおり、関数シグネチャを自動解析してツールのスキーマを生成し、 docstring を解析してツールおよび各引数の説明を抽出します。ポイントは次のとおりです。

1.  シグネチャ解析は `inspect` モジュールを使用します。型アノテーションで引数の型を把握し、動的に Pydantic モデルを構築して全体のスキーマを表現します。 Python プリミティブ型、 Pydantic モデル、 TypedDict などほとんどの型をサポートします。
2.  `griffe` で docstring を解析します。サポートフォーマットは `google`、`sphinx`、`numpy` です。フォーマットは自動検出を試みますがベストエフォートのため、 `function_tool` 呼び出し時に明示的に指定できます。`use_docstring_info` を `False` にすると docstring 解析を無効化できます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして利用

ワークフローによっては、ハンドオフせずに専門 エージェント のネットワークを中央の エージェント がオーケストレーションしたい場合があります。その場合、エージェント をツールとしてモデル化できます。

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

`agent.as_tool` は エージェント を簡単にツール化するための便利メソッドです。ただしすべての設定をサポートするわけではありません。たとえば `max_turns` は設定できません。高度なユースケースではツール実装内で `Runner.run` を直接呼び出してください。

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

### 出力のカスタム抽出

場合によっては、ツール化した エージェント の出力を中央 エージェント に返す前に加工したいことがあります。たとえば次のようなケースです。

-   サブエージェントのチャット履歴から特定の情報 (例: JSON ペイロード) を抽出したい。
-   エージェントの最終回答を変換・再フォーマットしたい (例: Markdown をプレーンテキストや CSV に変換)。
-   出力を検証し、エージェントの応答が欠落または不正な場合にフォールバック値を提供したい。

その場合は `as_tool` メソッドに `custom_output_extractor` 引数を渡してください。

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

## function ツールでのエラー処理

`@function_tool` で function ツールを作成する際、`failure_error_function` を渡せます。これはツール呼び出しがクラッシュした場合に LLM へエラーレスポンスを返す関数です。

-   既定では (何も渡さない場合)、`default_tool_error_function` が実行され、エラーが発生したことを LLM に伝えます。
-   独自のエラー関数を渡すと、その関数が実行され、その応答が LLM に送信されます。
-   明示的に `None` を渡すとツール呼び出しのエラーが再スローされます。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、ユーザーのコードがクラッシュした場合は `UserError` などが発生し得ます。

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 内でエラー処理を行う必要があります。