---
search:
  exclude: true
---
# ツール

ツールはエージェントに行動を取らせるための手段です。例えばデータの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作などが可能になります。Agents SDK には 3 種類のツールがあります。

-   ホスト型ツール: これらは LLM サーバー上で AI モデルと並行して動作します。OpenAI は retrieval、Web 検索、コンピュータ操作をホスト型ツールとして提供しています。  
-   Function calling: 任意の Python 関数をツールとして使用できます。  
-   エージェントをツールとして使用: ハンドオフを行わずに、エージェント同士が相互に呼び出せるようにします。  

## ホスト型ツール

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

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

## Function tools

任意の Python 関数をツールとして使用できます。Agents SDK が自動的に設定を行います。

-   ツール名は Python 関数名になります（または名前を指定可能）。  
-   ツールの説明は関数の docstring から取得されます（または説明を指定可能）。  
-   関数の引数から入力スキーマが自動生成されます。  
-   各入力の説明は docstring から取得されます（無効化可）。  

関数シグネチャの抽出には Python の `inspect` モジュールを使用し、docstring の解析には [`griffe`](https://mkdocstrings.github.io/griffe/)、スキーマ作成には `pydantic` を使用しています。

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

1.  引数には任意の Python 型を使用でき、関数は sync / async いずれでも構いません。  
2.  docstring があれば、説明や引数の説明を取得します。  
3.  関数の最初の引数として `context` を取ることができます。また、ツール名や説明、docstring スタイルなどのオーバーライドも設定可能です。  
4.  デコレーターを付けた関数をツールのリストに渡せます。  

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

### カスタム Function tool

Python 関数をそのままツールとして使いたくない場合は、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。必要なものは以下のとおりです。

-   `name`  
-   `description`  
-   `params_json_schema` : 引数の JSON スキーマ  
-   `on_invoke_tool` : [`ToolContext`][agents.tool_context.ToolContext] と引数（JSON 文字列）を受け取り、ツール出力を文字列で返す async 関数  

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

前述のとおり、関数シグネチャを解析してツールのスキーマを生成し、docstring を解析してツールおよび各引数の説明を取得します。ポイントは次のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。型アノテーションを利用して引数の型を理解し、Pydantic モデルを動的に構築してスキーマを表現します。Python プリミティブ、Pydantic モデル、TypedDict などほとんどの型をサポートします。  
2. docstring の解析には `griffe` を使用します。サポートされる docstring 形式は `google`、`sphinx`、`numpy` です。自動判定を試みますが、`function_tool` 呼び出し時に明示的に指定することもできます。`use_docstring_info` を `False` に設定すると docstring 解析を無効化できます。  

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして使用

ワークフローによっては、ハンドオフせずに中央のエージェントが専門エージェント群をオーケストレーションしたい場合があります。その際、エージェントをツールとしてモデル化できます。

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

`agent.as_tool` はエージェントを簡単にツール化するための便利メソッドです。ただし、すべての設定をサポートしているわけではありません。例えば `max_turns` は設定できません。高度なユースケースでは、ツール実装内で `Runner.run` を直接使用してください。

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

場合によっては、中央エージェントに返す前にツール化したエージェントの出力を加工したいことがあります。例えば次のようなケースです。

- サブエージェントのチャット履歴から特定の情報（例: JSON ペイロード）を抽出したい。  
- エージェントの最終回答を別形式に変換したい（例: Markdown → プレーンテキストや CSV）。  
- 出力を検証し、欠損・不正な場合にはフォールバック値を返したい。  

これを行うには、`as_tool` メソッドに `custom_output_extractor` 引数を渡します。

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

## Function tool 内のエラー処理

`@function_tool` で Function tool を作成する際、`failure_error_function` を渡すことができます。これはツール呼び出しがクラッシュした場合に LLM へエラー応答を提供する関数です。

-   何も渡さない場合は、デフォルトで `default_tool_error_function` が実行され、LLM にエラーが発生したことを伝えます。  
-   独自のエラー関数を渡した場合は、その関数が実行され、その結果が LLM に送信されます。  
-   明示的に `None` を渡した場合、ツール呼び出しのエラーは再送出されます。これには、モデルが無効な JSON を生成した場合の `ModelBehaviorError` や、コードがクラッシュした場合の `UserError` などが含まれます。  

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラー処理を行う必要があります。