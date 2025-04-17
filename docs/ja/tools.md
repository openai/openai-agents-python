---
search:
  exclude: true
---
# ツール

ツールはエージェントがアクションを実行できるようにします。たとえばデータの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作などです。Agents SDK には次の 3 種類のツールがあります。

-   ホストツール: これらは LLM サーバー上で AI モデルと一緒に実行されます。OpenAI は retrieval、Web 検索、コンピュータ操作をホストツールとして提供しています。
-   関数呼び出し: 任意の Python 関数をツールとして利用できます。
-   ツールとしてのエージェント: ハンドオフせずに、エージェントから他のエージェントを呼び出すことができます。

## ホストツール

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] はエージェントに Web 検索を行わせます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストアから情報を取得します。
-   [`ComputerTool`][agents.tool.ComputerTool] はコンピュータ操作タスクを自動化します。

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

任意の Python 関数をツールとして使用できます。Agents SDK が自動的に設定を行います。

-   ツールの名前は Python 関数の名前になります（任意で名前を指定することも可能です）
-   ツールの説明は関数の docstring から取得されます（任意で説明を指定することも可能です）
-   関数の引数から自動的に入力スキーマを生成します
-   各入力の説明は、無効化しない限り docstring から取得されます

Python の `inspect` モジュールを使用して関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、`pydantic` でスキーマを作成します。

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

1.  関数の引数には任意の Python 型を使用でき、同期・非同期どちらの関数も利用できます。
2.  docstring が存在する場合、ツールと引数の説明を取得します。
3.  関数はオプションで `context` を受け取れます（最初の引数である必要があります）。ツール名、説明、docstring のスタイルなどを上書き設定することも可能です。
4.  デコレートされた関数をツールのリストに渡してください。

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

Python 関数をそのままツールにしたくない場合は、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。次を指定する必要があります。

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON スキーマ）
-   `on_invoke_tool`（context と引数の JSON 文字列を受け取り、ツールの出力を文字列で返す async 関数）

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

前述のとおり、関数シグネチャを自動解析してツールのスキーマを生成し、docstring を解析してツールおよび個別引数の説明を抽出します。主な注意点は次のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。型アノテーションを用いて引数の型を認識し、Pydantic モデルを動的に構築して全体のスキーマを表現します。Python の基本型、Pydantic モデル、TypedDict などほとんどの型をサポートします。
2. `griffe` を使用して docstring を解析します。対応する docstring 形式は `google`、`sphinx`、`numpy` です。形式は自動検出を試みますが、`function_tool` 呼び出し時に明示的に指定することもできます。`use_docstring_info` を `False` に設定すると docstring 解析を無効化できます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしてのエージェント

一部のワークフローでは、ハンドオフせずに中央のエージェントが複数の専門エージェントをオーケストレーションしたい場合があります。そのような場合、エージェントをツールとしてモデル化できます。

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

`agent.as_tool` 関数はエージェントを簡単にツール化するためのヘルパーです。ただし、すべての設定に対応しているわけではありません（例: `max_turns` は設定不可）。高度なユースケースでは、ツール実装内で `Runner.run` を直接使用してください。

```python
@function_tool
async def run_my_agent() -> str:
  """A tool that runs the agent with custom configs".

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これはツール呼び出しが失敗した場合に LLM へ返すエラーレスポンスを生成する関数です。

-   何も指定しない場合、`default_tool_error_function` が実行され、LLM にエラー発生を伝えます。
-   独自のエラー関数を渡した場合はそちらが実行され、そのレスポンスが LLM へ送信されます。
-   明示的に `None` を渡すと、ツール呼び出し時のエラーは再送出されます。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などになります。

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。