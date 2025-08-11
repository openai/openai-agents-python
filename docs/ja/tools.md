---
search:
  exclude: true
---
# ツール

ツールは エージェント がアクションを実行できるようにします。たとえばデータ取得、コード実行、外部 API 呼び出し、そしてコンピュータ操作などです。Agents SDK には 3 つのツールクラスがあります。

-   ホスト型ツール: これらは LLM サーバー 上で AI モデルと一緒に動作します。OpenAI は retrieval、Web 検索、コンピュータ操作をホスト型ツールとして提供しています。
-   Function Calling: 任意の Python 関数 をツールとして利用できます。
-   エージェントをツールとして利用: エージェント をツールとして使用し、ハンドオフなしで他の エージェント を呼び出せます。

## ホスト型ツール

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は エージェント が Web 検索 を行えます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は OpenAI ベクトルストア から情報を取得します。
-   [`ComputerTool`][agents.tool.ComputerTool] は コンピュータ操作 タスクを自動化します。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は LLM がサンドボックス環境でコードを実行できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] はリモート MCP サーバー のツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] はプロンプトから画像を生成します。
-   [`LocalShellTool`][agents.tool.LocalShellTool] はローカルマシン上でシェルコマンドを実行します。

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

-   ツール名は Python 関数 の名前になります（任意で指定も可能）
-   ツール説明は関数の docstring から取得されます（任意で指定も可能）
-   関数入力のスキーマは関数の引数から自動生成されます
-   各入力の説明は、無効化しない限り docstring から取得されます

内部では Python の `inspect` モジュールでシグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、スキーマ生成に `pydantic` を使用します。

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

1.  関数の引数には任意の Python 型 を使用でき、同期・非同期のどちらでも構いません。  
2.  docstring が存在する場合、説明文および引数説明を抽出します。  
3.  関数は任意で `context` を受け取れます（最初の引数である必要があります）。また、ツール名・説明・docstring スタイルなどのオーバーライドを設定できます。  
4.  デコレートした関数を tools のリストに渡せます。  

??? note "出力を表示するには展開"

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

Python 関数 をそのままツールにしたくない場合は、直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。次を指定する必要があります。

-   `name`
-   `description`
-   `params_json_schema` : 引数用の JSON スキーマ
-   `on_invoke_tool` : [`ToolContext`][agents.tool_context.ToolContext] と JSON 文字列の引数を受け取り、ツール出力を文字列で返す async 関数

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

前述のとおり、関数シグネチャを自動解析してツールのスキーマを生成し、docstring からツールと各引数の説明を抽出します。ポイントは次のとおりです。

1. シグネチャ解析は `inspect` モジュールで実施します。型アノテーションを利用して引数の型を把握し、Pydantic モデルを動的に構築して全体スキーマを表現します。Python の基本型、Pydantic モデル、TypedDict など多くの型をサポートします。  
2. docstring 解析には `griffe` を使用します。対応フォーマットは `google`、`sphinx`、`numpy` です。ドキュメント形式は自動推定しますがベストエフォートであり、`function_tool` 呼び出し時に明示できます。`use_docstring_info` を `False` にすると解析を無効化できます。  

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしてのエージェント

いくつかのワークフローでは、ハンドオフせずに中心となる エージェント が専門 エージェント のネットワークをオーケストレーションしたい場合があります。エージェント をツールとしてモデル化することでこれが可能です。

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

`agent.as_tool` 関数は、エージェント を簡単にツール化するための便利メソッドです。ただしすべての設定をサポートするわけではありません。たとえば `max_turns` は設定できません。高度なユースケースでは、ツール実装内で `Runner.run` を直接使用してください。

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

### 出力抽出のカスタマイズ

場合によっては、ツールエージェント の出力を中央 エージェント に返す前に加工したいことがあります。たとえば以下のようなケースです。

- サブエージェント のチャット履歴から特定情報（例: JSON ペイロード）を抽出したい  
- エージェント の最終回答を変換・再フォーマットしたい（例: Markdown をプレーンテキストや CSV に変換）  
- 出力を検証し、エージェント の応答が欠落・不正な場合にフォールバック値を提供したい  

これらは `as_tool` メソッドに `custom_output_extractor` 引数を渡すことで実現できます。

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

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これはツール呼び出しがクラッシュした場合に LLM へエラーレスポンスを返す関数です。

-   既定では何も渡さない場合、`default_tool_error_function` が実行され、LLM にエラーが発生したことを通知します。
-   独自のエラー関数を渡した場合はそれが実行され、その結果が LLM に送信されます。
-   明示的に `None` を渡すと、ツール呼び出しエラーが再送出されます。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、ユーザーコードがクラッシュした場合は `UserError` などになります。

手動で `FunctionTool` オブジェクトを作成する場合、`on_invoke_tool` 関数内でエラー処理を行う必要があります。