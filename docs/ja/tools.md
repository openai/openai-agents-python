# ツール

ツールは エージェント がアクションを実行するためのものです。たとえば、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作などが含まれます。Agents SDK には 3 種類のツールクラスがあります。

-   Hosted tools: これらは LLM サーバー上で AI モデルとともに実行されます。OpenAI は retrieval、Web 検索、コンピュータ操作を Hosted tools として提供しています。
-   Function calling: 任意の Python 関数をツールとして利用できます。
-   Agents as tools: エージェントをツールとして利用でき、エージェントが他のエージェントをハンドオフせずに呼び出すことができます。

## Hosted tools

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は、エージェントが Web 検索を行うことを可能にします。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は、OpenAI ベクトルストアから情報を取得できます。
-   [`ComputerTool`][agents.tool.ComputerTool] は、コンピュータ操作タスクの自動化を可能にします。

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

任意の Python 関数をツールとして利用できます。Agents SDK が自動的にツールをセットアップします。

-   ツール名は Python 関数名になります（または任意の名前を指定できます）
-   ツールの説明は関数の docstring から取得されます（または任意の説明を指定できます）
-   関数の引数から自動的に入力スキーマが作成されます
-   各入力の説明は、関数の docstring から取得されます（無効化も可能です）

Python の `inspect` モジュールを使って関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、`pydantic` でスキーマを作成します。

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

1.  関数の引数には任意の Python 型を利用でき、同期・非同期どちらの関数も利用可能です。
2.  docstring があれば、説明や引数の説明として利用されます。
3.  関数はオプションで `context` を最初の引数として受け取れます。また、ツール名や説明、docstring スタイルの指定などのオーバーライドも可能です。
4.  デコレートした関数をツールのリストに渡すことができます。

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

### カスタム function tools

場合によっては、Python 関数をツールとして使いたくないこともあります。その場合は、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。必要な情報は以下の通りです。

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON スキーマ）
-   `on_invoke_tool`（context と引数（JSON 文字列）を受け取り、ツールの出力を文字列で返す async 関数）

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

前述の通り、関数シグネチャを自動解析してツールのスキーマを抽出し、docstring からツールや各引数の説明を抽出します。主なポイントは以下の通りです。

1. シグネチャの解析は `inspect` モジュールで行います。型アノテーションを利用して引数の型を把握し、Pydantic モデルを動的に構築して全体のスキーマを表現します。Python の基本コンポーネント、Pydantic モデル、TypedDict など、ほとんどの型をサポートしています。
2. docstring の解析には `griffe` を使用します。サポートされている docstring フォーマットは `google`、`sphinx`、`numpy` です。docstring フォーマットは自動検出を試みますが、`function_tool` 呼び出し時に明示的に指定することもできます。`use_docstring_info` を `False` に設定することで docstring 解析を無効化できます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## Agents as tools

一部のワークフローでは、ハンドオフせずに中央のエージェントが専門エージェントのネットワークをオーケストレーションしたい場合があります。その場合、エージェントをツールとしてモデル化することで実現できます。

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

## Function tools でのエラー処理

`@function_tool` で function tool を作成する際、`failure_error_function` を渡すことができます。これは、ツール呼び出しがクラッシュした場合に LLM へエラー応答を提供する関数です。

-   デフォルト（何も渡さない場合）では、`default_tool_error_function` が実行され、LLM にエラーが発生したことを伝えます。
-   独自のエラー関数を渡した場合は、それが実行され、その応答が LLM に送信されます。
-   明示的に `None` を渡した場合、ツール呼び出し時のエラーは再スローされ、ユーザー側で処理できます。たとえば、モデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などです。

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラー処理を行う必要があります。