---
search:
  exclude: true
---
# ツール

ツールはエージェントがアクションを実行できるようにします。データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作などです。 Agents SDK にはツールのクラスが 3 つあります:

-   ホスト型ツール: これらは AI モデルと同じ LLM サーバー上で動作します。 OpenAI は、リトリーバル、 Web 検索、コンピュータ操作をホスト型ツールとして提供しています。
-   Function calling: 任意の Python 関数をツールとして使えるようにします。
-   エージェントをツールとして: エージェントをツールとして使えるため、ハンドオフせずにエージェントが他のエージェントを呼び出せます。

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する場合、 OpenAI はいくつかの組み込みツールを提供しています:

-   [`WebSearchTool`][agents.tool.WebSearchTool] は、エージェントが Web を検索できるようにします。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は、 OpenAI のベクトルストアから情報を取得できます。
-   [`ComputerTool`][agents.tool.ComputerTool] は、コンピュータ操作タスクの自動化を可能にします。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は、 LLM がサンドボックス化された環境でコードを実行できるようにします。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。
-   [`LocalShellTool`][agents.tool.LocalShellTool] は、ローカルマシン上でシェルコマンドを実行します。

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

任意の Python 関数をツールとして使用できます。 Agents SDK がツールを自動的にセットアップします:

-   ツール名は Python 関数名になります（または任意の名前を指定できます）。
-   ツールの説明は関数のドックストリングから取得されます（または説明を指定できます）。
-   関数の入力のスキーマは、関数の引数から自動的に作成されます。
-   各入力の説明は、無効化しない限り関数のドックストリングから取得されます。

関数シグネチャの抽出には Python の `inspect` モジュールを使用し、ドックストリングの解析には [`griffe`](https://mkdocstrings.github.io/griffe/) を、スキーマの作成には `pydantic` を使用します。

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

1.  任意の Python 型を関数の引数に使え、関数は同期/非同期のいずれでも構いません。
2.  ドックストリングがあれば、説明や引数の説明として利用されます。
3.  関数は任意で `context` を受け取れます（先頭の引数である必要があります）。また、ツール名や説明、使用するドックストリングのスタイルなどのオーバーライドも設定できます。
4.  デコレートした関数をツールのリストに渡せます。

??? note "展開して出力を表示"

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

場合によっては、 Python 関数をツールとして使いたくないこともあります。必要に応じて、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。次を指定する必要があります:

-   `name`
-   `description`
-   `params_json_schema`（引数のための JSON スキーマ）
-   `on_invoke_tool`（[`ToolContext`][agents.tool_context.ToolContext] と引数を JSON 文字列で受け取り、ツールの出力を文字列で返す非同期関数）

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

### 引数とドックストリングの自動解析

前述のとおり、ツールのスキーマを得るために関数シグネチャを自動解析し、ツールおよび各引数の説明を得るためにドックストリングも解析します。注意点は次のとおりです:

1. `inspect` モジュールでシグネチャを解析します。引数の型は型アノテーションから解釈し、全体のスキーマを表す Pydantic モデルを動的に構築します。 Python の基本型、 Pydantic モデル、 TypedDict など、ほとんどの型をサポートします。
2.  ドックストリングの解析には `griffe` を使用します。対応するドックストリング形式は `google`、`sphinx`、`numpy` です。ドックストリング形式は自動検出を試みますがベストエフォートのため、`function_tool` を呼び出す際に明示的に指定できます。`use_docstring_info` を `False` に設定して、ドックストリング解析を無効化することも可能です。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしてのエージェント

一部のワークフローでは、制御をハンドオフするのではなく、中核のエージェントが特化したエージェント群をオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデリングすることで実現できます。

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

`agent.as_tool` 関数は、エージェントを簡単にツール化できるようにする簡便なメソッドです。ただし、すべての設定をサポートしているわけではありません。例えば `max_turns` は設定できません。高度なユースケースでは、ツールの実装内で `Runner.run` を直接使用してください:

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

場合によっては、中央のエージェントに返す前にツール化したエージェントの出力を加工したいことがあります。例えば次のような場合に有用です:

- 特定の情報（例: JSON ペイロード）をサブエージェントのチャット履歴から抽出する。
- エージェントの最終回答を変換または再整形する（例: Markdown をプレーンテキストや CSV に変換）。
- 出力を検証する、またはエージェントの応答が欠落している/形式不正な場合にフォールバック値を提供する。

これは、`as_tool` メソッドに `custom_output_extractor` 引数を渡すことで実現できます:

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

`@function_tool` で関数ツールを作成する際は、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へエラーレスポンスを返す関数です。

-   既定では（何も渡さない場合）、`default_tool_error_function` が実行され、 LLM にエラーが発生したことを伝えます。
-   独自のエラー関数を渡した場合はそれが実行され、そのレスポンスが LLM に送信されます。
-   明示的に `None` を渡すと、ツール呼び出しのエラーは再送出され、呼び出し側で処理する必要があります。モデルが不正な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などになり得ます。

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

手動で `FunctionTool` オブジェクトを作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。