# ツール

ツールはエージェントがアクションを実行するためのものです。データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータの使用などが含まれます。Agent SDK には3つのクラスのツールがあります。

-   ホストされたツール: これらは LLM サーバー上で AI モデルと一緒に実行されます。OpenAI は、リトリーバル、Web 検索、コンピュータ操作をホストされたツールとして提供しています。
-   関数呼び出し: 任意の Python 関数をツールとして使用できます。
-   エージェントをツールとして使用: エージェントをツールとして使用し、エージェントが他のエージェントをハンドオフせずに呼び出すことができます。

## ホストされたツール

OpenAI は、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際にいくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] はエージェントが Web 検索を行うことを可能にします。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストアから情報を取得することを可能にします。
-   [`ComputerTool`][agents.tool.ComputerTool] はコンピュータ操作タスクを自動化することを可能にします。

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

任意の Python 関数をツールとして使用できます。Agents SDK はツールを自動的にセットアップします。

-   ツールの名前は Python 関数の名前になります（または名前を指定できます）
-   ツールの説明は関数の docstring から取得されます（または説明を指定できます）
-   関数入力のスキーマは関数の引数から自動的に作成されます
-   各入力の説明は、無効にしない限り、関数の docstring から取得されます

Python の `inspect` モジュールを使用して関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) を使用して docstring を解析し、`pydantic` を使用してスキーマを作成します。

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

1.  任意の Python 型を関数の引数として使用でき、関数は同期または非同期であることができます。
2.  Docstring が存在する場合、説明や引数の説明を取得するために使用されます。
3.  関数はオプションで `context` を取ることができ（最初の引数である必要があります）、ツールの名前、説明、使用する docstring スタイルなどのオーバーライドを設定できます。
4.  デコレートされた関数をツールのリストに渡すことができます。

??? note "出力を表示するには展開してください"

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

時には、Python 関数をツールとして使用したくない場合もあります。その場合、直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。以下を提供する必要があります。

-   `name`
-   `description`
-   `params_json_schema`（引数の JSON スキーマ）
-   `on_invoke_tool`（コンテキストと引数を JSON 文字列として受け取り、ツールの出力を文字列として返す非同期関数）

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

前述のように、ツールのスキーマを抽出するために関数シグネチャを自動的に解析し、ツールおよび個々の引数の説明を抽出するために docstring を解析します。以下の点に注意してください。

1. シグネチャの解析は `inspect` モジュールを通じて行われます。引数の型を理解するために型注釈を使用し、全体のスキーマを表す Pydantic モデルを動的に構築します。Python の基本コンポーネント、Pydantic モデル、TypedDict など、ほとんどの型をサポートしています。
2. `griffe` を使用して docstring を解析します。サポートされている docstring フォーマットは `google`、`sphinx`、`numpy` です。docstring フォーマットを自動的に検出しようとしますが、これはベストエフォートであり、`function_tool` を呼び出す際に明示的に設定できます。`use_docstring_info` を `False` に設定することで docstring 解析を無効にすることもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## エージェントをツールとして使用

一部のワークフローでは、中央のエージェントが専門エージェントのネットワークをオーケストレーションすることを望むかもしれません。これを行うには、エージェントをツールとしてモデル化します。

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

## 関数ツールでのエラー処理

`@function_tool` を使用して関数ツールを作成する際、`failure_error_function` を渡すことができます。これは、ツール呼び出しがクラッシュした場合に LLM にエラーレスポンスを提供する関数です。

-   デフォルトでは（何も渡さない場合）、`default_tool_error_function` が実行され、LLM にエラーが発生したことを伝えます。
-   独自のエラー関数を渡した場合、それが実行され、LLM にレスポンスが送信されます。
-   明示的に `None` を渡した場合、ツール呼び出しエラーは再度発生し、処理する必要があります。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などが考えられます。

`FunctionTool` オブジェクトを手動で作成する場合、`on_invoke_tool` 関数内でエラーを処理する必要があります。