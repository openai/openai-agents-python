---
search:
  exclude: true
---
# ツール

ツールはエージェントが行動を起こすための手段です。例えばデータ取得、コード実行、外部 API 呼び出し、さらにはコンピュータ操作などが可能になります。 Agents SDK には 3 つのツールクラスがあります。

-   ホステッドツール: これらは LLM サーバー上で AI モデルと並行して実行されます。 OpenAI はリトリーバル、 Web 検索、コンピュータ操作をホステッドツールとして提供しています。  
-   関数呼び出し: 任意の Python 関数をツールとして利用できます。  
-   エージェントをツールとして使用: ハンドオフせずにエージェント同士を呼び出せるよう、エージェント自体をツールとして扱えます。  

## ホステッドツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用すると、 OpenAI はいくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] はエージェントに Web 検索を行わせます。  
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストアから情報を取得します。  
-   [`ComputerTool`][agents.tool.ComputerTool] はコンピュータ操作タスクを自動化します。  
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] はサンドボックス環境でコードを実行します。  
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

任意の Python 関数をツールとして利用できます。 Agents SDK が自動でセットアップします。

-   ツール名は Python 関数名になります (任意で指定も可能)  
-   ツールの説明は関数の docstring から取得されます (任意で指定も可能)  
-   関数入力のスキーマは関数の引数から自動生成されます  
-   各入力の説明は docstring から取得され、無効化も可能です  

Python の `inspect` モジュールで関数シグネチャを抽出し、 docstring 解析には [`griffe`](https://mkdocstrings.github.io/griffe/) を、スキーマ生成には `pydantic` を使用しています。

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

1.  関数の引数には任意の Python 型を使え、同期・非同期どちらの関数でも構いません。  
2.  Docstring があれば、ツール説明と引数説明を取得します。  
3.  関数は任意で `context` を最初の引数として受け取れます。また、ツール名や説明、 docstring スタイルなどの上書き設定も可能です。  
4.  デコレートした関数をツール一覧に渡すだけで利用できます。  

??? note "展開して出力を確認"

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

Python 関数を使わずにツールを作成したい場合は、直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。次の項目を指定してください。

-   `name`  
-   `description`  
-   `params_json_schema` — 引数用 JSON スキーマ  
-   `on_invoke_tool` — [`ToolContext`][agents.tool_context.ToolContext] と引数 (JSON 文字列) を受け取り、ツール出力を文字列で返す非同期関数  

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

前述のとおり、ツールのスキーマは関数シグネチャを自動解析して抽出し、 docstring からツール説明および各引数の説明を取得します。注意点は以下のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。型アノテーションを利用して引数の型を理解し、全体スキーマを表す Pydantic モデルを動的に構築します。 Python のプリミティブ型、 Pydantic モデル、 TypedDict などほとんどの型をサポートします。  
2. `griffe` で docstring を解析します。サポートする docstring 形式は `google`、`sphinx`、`numpy` です。 docstring 形式は自動検出を試みますが、 `function_tool` 呼び出し時に明示設定もできます。 `use_docstring_info` を `False` にすると docstring 解析を無効化できます。  

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして使う

一部のワークフローでは、制御をハンドオフせずに中央のエージェントが複数の専門エージェントをオーケストレーションしたい場合があります。その際にはエージェントをツールとしてモデル化できます。

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

`agent.as_tool` はエージェントを簡単にツール化するための便利メソッドです。ただしすべての設定をサポートしているわけではなく、たとえば `max_turns` を設定できません。より高度なケースでは、ツール実装内で `Runner.run` を直接呼び出してください。

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

必要に応じて、ツールエージェントの出力を中央エージェントへ返す前に加工できます。例としては次のようなケースです。

- サブエージェントのチャット履歴から特定情報 (例: JSON ペイロード) を抽出したい場合  
- エージェントの最終回答を変換・再フォーマットしたい場合 (例: Markdown をプレーンテキストや CSV へ変換)  
- 出力を検証し、欠落または不正な場合にフォールバック値を提供したい場合  

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

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、 `failure_error_function` を渡せます。これはツール呼び出しが失敗したときに LLM へ返すエラーレスポンスを生成する関数です。

-   何も渡さなかった場合、デフォルトで `default_tool_error_function` が実行され、 LLM にエラーが発生したことを伝えます。  
-   独自のエラー関数を渡した場合、その関数が実行され、その結果が LLM に送信されます。  
-   明示的に `None` を渡すと、ツール呼び出しエラーは再送出されます。たとえばモデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などが発生します。  

`FunctionTool` オブジェクトを手動で作成する場合は、 `on_invoke_tool` 内でエラー処理を行う必要があります。