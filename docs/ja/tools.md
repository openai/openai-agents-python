---
search:
  exclude: true
---
# ツール

ツールはエージェントに、データ取得、コード実行、外部 API 呼び出し、さらには コンピュータ操作 まで、さまざまなアクションを実行させます。Agents SDK には、次の 3 つのクラスのツールがあります。

- ホスト型ツール: これらは LLM サーバー 上で AI モデルと並行して実行されます。OpenAI は retrieval、Web 検索、コンピュータ操作 をホスト型ツールとして提供しています。
- Function Calling: あらゆる Python 関数 をツールとして利用できます。
- エージェントをツールとして使用: エージェント自身をツールとして扱い、ハンドオフなしに他のエージェントを呼び出せます。

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際、OpenAI にはいくつかの組み込みツールがあります。

- [`WebSearchTool`][agents.tool.WebSearchTool] はエージェントに Web 検索 を行わせます。
- [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストア から情報を取得します。
- [`ComputerTool`][agents.tool.ComputerTool] は コンピュータ操作 タスクを自動化します。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は LLM がサンドボックス環境でコードを実行できるようにします。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] はリモート MCP サーバー のツールをモデルに公開します。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool] はプロンプトから画像を生成します。
- [`LocalShellTool`][agents.tool.LocalShellTool] はローカルマシンでシェルコマンドを実行します。

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

任意の Python 関数 をツールとして利用できます。Agents SDK が自動的にセットアップを行います。

- ツール名は Python 関数 の名前になります（明示的に指定することも可能）
- ツールの説明は関数の docstring から取得されます（こちらも上書き可能）
- 関数引数から入力スキーマが自動生成されます
- 各入力の説明は docstring から取得されます（無効化も可能）

Python の `inspect` モジュールでシグネチャを取得し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析、`pydantic` でスキーマを作成しています。

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

1. 関数引数には任意の Python 型 を使用でき、同期関数でも非同期関数でも構いません。  
2. docstring があれば、ツールおよび各引数の説明を取得します。  
3. 関数はオプションで `context`（先頭の引数）を受け取れます。また、ツール名や説明、docstring スタイルなどの上書きも可能です。  
4. 修飾された関数を tools のリストに渡してください。  

??? note "クリックして出力を表示"

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

Python 関数 をそのままツールにしたくない場合は、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。必要な項目は以下のとおりです。

- `name`
- `description`
- `params_json_schema`（引数の JSON スキーマ）
- `on_invoke_tool`（[`ToolContext`][agents.tool_context.ToolContext] と引数の JSON 文字列を受け取り、ツールの出力文字列を返す async 関数）

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

### 引数および docstring の自動解析

前述のとおり、関数シグネチャと docstring を自動解析してツールのスキーマと説明を生成します。主なポイントは次のとおりです。

1. `inspect` によりシグネチャを解析し、型アノテーションから Pydantic モデル を動的に生成します。Python の基本型、Pydantic モデル、TypedDict など多くの型をサポートします。  
2. `griffe` で docstring を解析します。対応フォーマットは `google`、`sphinx`、`numpy` です。自動判定はベストエフォートで行われますが、`function_tool` 呼び出し時に明示的に指定することも、`use_docstring_info` を `False` にして無効化することも可能です。  

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしてのエージェント

ワークフローによっては、中央のエージェントが複数の専門エージェントをオーケストレーションし、ハンドオフせずに制御を維持したい場合があります。その場合、エージェントをツールとしてモデル化します。

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

`agent.as_tool` はエージェントを簡単にツール化するための便利メソッドですが、`max_turns` などすべての設定をサポートしているわけではありません。高度なユースケースでは、ツール実装内で `Runner.run` を直接呼び出してください。

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

ツールエージェントの出力を中央エージェントへ返す前に加工したい場合があります。たとえば次のようなケースです。

- サブエージェントのチャット履歴から特定の情報（例: JSON ペイロード）だけを抽出したい  
- エージェントの最終回答を変換・再フォーマットしたい（例: Markdown → プレーンテキストや CSV）  
- 出力を検証し、欠落または不正な場合にフォールバック値を返したい  

このような場合は、`as_tool` メソッドの `custom_output_extractor` 引数に関数を渡します。

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

## 関数ツールのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。この関数は、ツール呼び出しがクラッシュしたときに LLM へ返すエラーレスポンスを生成します。

- 省略した場合は `default_tool_error_function` が実行され、LLM にエラーが発生したことを通知します。  
- 独自のエラー関数を渡した場合は、それが実行され、その結果が LLM へ送信されます。  
- 明示的に `None` を渡すと、ツール呼び出しのエラーは再送出されます。たとえば、モデルが無効な JSON を生成した場合は `ModelBehaviorError`、ユーザーコードがクラッシュした場合は `UserError` などです。  

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 内でエラー処理を実装する必要があります。