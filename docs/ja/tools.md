---
search:
  exclude: true
---
# ツール

ツールを利用することで エージェント は、データの取得、コードの実行、外部 API の呼び出し、さらには コンピュータ操作 までも行えます。 Agents SDK には 3 種類のツールがあります：

-   ホスト型ツール: これらは LLM サーバー上で AI モデルと並行して実行されます。OpenAI は retrieval、 Web 検索、 コンピュータ操作 をホスト型ツールとして提供しています。
-   Function calling:  任意の Python 関数をツールとして利用できます。
-   エージェントをツールとして使用: これにより、ハンドオフせずに エージェント 同士を呼び出すことができます。

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際、OpenAI にはいくつかの組み込みツールがあります:

-   [`WebSearchTool`][agents.tool.WebSearchTool] は エージェント に Web 検索 を行わせます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストア から情報を取得します。
-   [`ComputerTool`][agents.tool.ComputerTool] は コンピュータ操作 タスクを自動化します。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は サンドボックス環境でコードを実行します。
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

## 関数ツール

任意の Python 関数をツールとして利用できます。 Agents SDK が自動的に設定を行います:

-   ツール名には Python 関数名が使用されます（または自分で指定可能）
-   ツールの説明は関数の docstring から取得されます（または自分で指定可能）
-   関数入力のスキーマは関数の引数から自動生成されます
-   各入力の説明は docstring から取得されます（無効化も可能）

Python の `inspect` モジュールで関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、`pydantic` でスキーマを生成しています。

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

1.  関数は同期・非同期のどちらでも良く、引数には任意の Python 型を使用できます。
2.  docstring があれば、ツール説明や引数説明を取得します。
3.  関数は任意で `context`（先頭の引数）を受け取れます。ツール名や説明、docstring スタイルなどをオーバーライドすることもできます。
4.  デコレートした関数をツールのリストに渡してください。

??? note "出力を確認するには展開"

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

### カスタム関数ツール

Python 関数を使わずにツールを作成したい場合は、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。以下を指定してください:

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON スキーマ）
-   `on_invoke_tool`（[`ToolContext`][agents.tool_context.ToolContext] と引数 JSON 文字列を受け取り、文字列としてツールの出力を返す async 関数）

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

前述のとおり、関数シグネチャを自動解析してツールのスキーマを生成し、docstring からツール説明や各引数の説明を抽出します。ポイントは以下の通りです:

1. シグネチャ解析は `inspect` モジュールで行います。型アノテーションから引数の型を把握し、動的に Pydantic モデルを作成します。Python の基本型、Pydantic モデル、TypedDict など大半の型をサポートします。
2. `griffe` で docstring を解析します。対応フォーマットは `google`, `sphinx`, `numpy` です。フォーマットは自動検出を試みますが、`function_tool` 呼び出し時に明示的に設定することも可能です。`use_docstring_info` を `False` にすると docstring 解析を無効にできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして使用

ワークフローによっては、ハンドオフせずに中央の エージェント が複数の専門 エージェント をオーケストレーションしたい場合があります。その際は エージェント をツールとしてモデル化できます。

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

### ツール エージェント のカスタマイズ

`agent.as_tool` は エージェント を簡単にツール化するための便利メソッドです。ただし、`max_turns` などすべての設定をサポートしているわけではありません。高度なユースケースでは、ツール実装内で `Runner.run` を直接使用してください:

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

場合によっては、ツール エージェント の出力を中央 エージェント に返す前に加工したいことがあります。たとえば、以下のようなケースです:

- サブ エージェント のチャット履歴から特定の情報（例: JSON ペイロード）だけを抽出する。
- エージェント の最終回答を変換・再フォーマットする（例: Markdown をプレーンテキストや CSV に変換）。
- エージェント の応答が欠落している、または不正な場合に検証やフォールバック値を提供する。

その場合は `as_tool` メソッドに `custom_output_extractor` 引数を渡してください:

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

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡すことができます。これはツール呼び出しがクラッシュしたときに LLM にエラーレスポンスを返す関数です。

-   何も渡さない場合は既定の `default_tool_error_function` が実行され、LLM にエラーが発生したことを知らせます。
-   独自のエラーファンクションを渡すと、それが実行され、LLM にそのレスポンスが送信されます。
-   明示的に `None` を渡した場合、ツール呼び出しエラーは再スローされます。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、自分のコードがクラッシュした場合は `UserError` などになります。

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 内でエラーを処理する必要があります。