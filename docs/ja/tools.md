---
search:
  exclude: true
---
# ツール

エージェントはツールを利用することで、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作まで多彩なアクションを実行できます。Agents SDK には次の 3 つのツールクラスがあります。

-   Hosted ツール: これらは LLM サーバー上で AI モデルと並行して動作します。OpenAI は retrieval、Web 検索、コンピュータ操作 を Hosted ツールとして提供しています。
-   Function calling: 任意の Python 関数をツールとして利用できます。
-   Agents as tools: エージェント自体をツール化することで、ハンドオフすることなくエージェント同士を呼び出せます。

## Hosted ツール

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] により、エージェントは Web 検索 を実行できます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストアから情報を取得します。
-   [`ComputerTool`][agents.tool.ComputerTool] はコンピュータ操作タスクを自動化します。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は LLM がサンドボックス環境でコードを実行できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] はリモート MCP サーバーのツールをモデルへ公開します。
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

任意の Python 関数をそのままツールとして利用できます。Agents SDK が自動で設定を行います。

-   ツール名には Python 関数名が使用されます (別名を指定することも可能)。
-   ツール説明は関数の docstring から取得されます (カスタム説明も可)。
-   関数の引数から自動で入力スキーマを生成します。
-   各入力の説明は docstring から取得されます (無効化も可能)。

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

1.  引数には任意の Python 型を使用でき、同期／非同期どちらの関数もサポートします。  
2.  docstring があれば、ツールおよび引数の説明を取得します。  
3.  関数の最初の引数に `context` を受け取ることができ、ツール名や説明、docstring スタイルなどのオーバーライドも設定可能です。  
4.  デコレートした関数を tools のリストに渡すだけで利用できます。  

??? note "出力を展開して表示"

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

Python 関数を使わずに [`FunctionTool`][agents.tool.FunctionTool] を直接作成することもできます。その場合、次を指定してください。

-   `name`
-   `description`
-   `params_json_schema` ― 引数の JSON スキーマ
-   `on_invoke_tool` ― [`ToolContext`][agents.tool_context.ToolContext] と引数(JSON 文字列)を受け取り、ツール出力を文字列で返す async 関数

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

前述のとおり、関数シグネチャを解析してスキーマを生成し、docstring からツール説明や各引数の説明を抽出します。ポイントは以下のとおりです。

1. `inspect` モジュールでシグネチャを解析し、型アノテーションから Pydantic モデルを動的に生成します。Python の基本型、Pydantic モデル、TypedDict など大半の型をサポートします。  
2. `griffe` で docstring を解析します。サポートする形式は `google`、`sphinx`、`numpy` です。自動判定はベストエフォートのため、`function_tool` 呼び出し時に明示設定もできます。`use_docstring_info` を `False` にすると解析を無効化できます。  

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## Agents as tools

ワークフローによっては、複数の専門エージェントを中央のエージェントがオーケストレーションする方が便利な場合があります。その際、エージェントをツールとして扱うことでハンドオフせずに実現できます。

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

`agent.as_tool` はエージェントを手軽にツール化するためのヘルパーです。ただし `max_turns` などすべての設定をサポートしているわけではありません。高度な構成が必要な場合は、ツール実装内で `Runner.run` を直接呼び出してください。

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

ツール化したエージェントの出力を中央エージェントへ返す前に加工したいケースがあります。たとえば以下のような場合です。

- チャット履歴から特定の情報 (例: JSON ペイロード) のみを抽出したい。  
- Markdown をプレーンテキストや CSV に変換するなど、最終回答のフォーマットを変更したい。  
- レスポンスが欠落・不正な場合に検証やフォールバック値を設定したい。  

`as_tool` メソッドの `custom_output_extractor` 引数に関数を渡すことで実現できます。

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

`@function_tool` でツールを作成する際、`failure_error_function` を指定できます。これはツール呼び出しが失敗した場合に LLM へ返すエラーメッセージを生成する関数です。

-   何も渡さなかった場合、デフォルトの `default_tool_error_function` が実行され、「エラーが発生した」と LLM へ通知します。  
-   独自のエラー関数を渡すと、それが実行され LLM へ送信されます。  
-   明示的に `None` を渡すと、ツール呼び出し時のエラーは再スローされ、呼び出し側で処理できます。たとえばモデルが無効な JSON を生成した場合は `ModelBehaviorError`、ユーザーコードがクラッシュした場合は `UserError` などです。  

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 内でエラーを処理する必要があります。