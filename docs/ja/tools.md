---
search:
  exclude: true
---
# ツール

ツールは エージェント にアクションを実行させます。データ取得、コード実行、外部 API 呼び出し、さらにはコンピュータ操作 などが含まれます。Agents SDK には 3 つのツールのクラスがあります:

- Hosted tools: これは AI モデルと同じ LLM サーバー 上で動作します。OpenAI は retrieval、Web 検索、computer use を Hosted tools として提供しています。
- Function calling: 任意の Python 関数をツールとして利用できます。
- Agents as tools: エージェント をツールとして利用でき、ハンドオフ せずに他の エージェント を呼び出せます。

## Hosted tools

OpenAI は [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています:

- [`WebSearchTool`][agents.tool.WebSearchTool]: エージェント が Web 検索 を実行します。
- [`FileSearchTool`][agents.tool.FileSearchTool]: OpenAI ベクトルストア から情報を取得できます。
- [`ComputerTool`][agents.tool.ComputerTool]: コンピュータ操作 の自動化を行います。
- [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool]: LLM がサンドボックス環境でコードを実行できます。
- [`HostedMCPTool`][agents.tool.HostedMCPTool]: リモートの MCP サーバー のツールをモデルに公開します。
- [`ImageGenerationTool`][agents.tool.ImageGenerationTool]: プロンプトから画像を生成します。
- [`LocalShellTool`][agents.tool.LocalShellTool]: ローカルマシン上でシェルコマンドを実行します。

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

任意の Python 関数をツールとして使用できます。Agents SDK が自動的にツールをセットアップします:

- ツール名は Python 関数名になります（任意で名前を指定可能）
- ツールの説明は関数の docstring から取得されます（任意で説明を指定可能）
- 関数入力のスキーマは、関数の引数から自動生成されます
- 各入力引数の説明は、無効化しない限り関数の docstring から取得されます

Python の `inspect` モジュールで関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、スキーマ作成には `pydantic` を使用します。

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

1. 関数の引数には任意の Python 型を使用でき、関数は同期・非同期どちらでも構いません。
2. docstring があれば、説明および引数の説明として利用します。
3. 関数は任意で `context` を最初の引数として受け取れます。ツール名、説明、docstring スタイルなどの上書き設定も可能です。
4. デコレートした関数をツールのリストに渡せます。

??? note "出力を見るには展開してください"

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

### 関数ツールから画像やファイルを返す

テキスト出力に加えて、関数ツールの出力として 1 つ以上の画像やファイルを返すことができます。そのためには次のいずれかを返します:

- 画像: [`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
- ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
- テキスト: 文字列、文字列化可能なオブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

Python 関数をツールとして使いたくない場合もあります。必要であれば直接 [`FunctionTool`][agents.tool.FunctionTool] を作成できます。次を用意する必要があります:

- `name`
- `description`
- 引数の JSON スキーマである `params_json_schema`
- [`ToolContext`][agents.tool_context.ToolContext] と引数（JSON 文字列）を受け取り、ツール出力を文字列で返す非同期関数 `on_invoke_tool`

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

前述のとおり、ツールのスキーマ抽出のために関数シグネチャを自動解析し、ツールおよび各引数の説明抽出のために docstring を解析します。注意点は次のとおりです:

1. シグネチャ解析は `inspect` モジュールで行います。引数の型は型アノテーションから理解し、全体スキーマを表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict など大半の型をサポートします。
2. docstring の解析には `griffe` を使用します。サポートする docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式は自動検出を試みますがベストエフォートのため、`function_tool` 呼び出し時に明示的に指定できます。`use_docstring_info` を `False` に設定すると docstring 解析を無効化できます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

## ツールとしての エージェント

一部のワークフローでは、ハンドオフ せずに、中央の エージェント が専門 エージェント のネットワークをオーケストレーションしたい場合があります。これは エージェント をツールとしてモデル化することで実現できます。

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

`agent.as_tool` は エージェント をツールに変換するための簡便メソッドです。ただし、すべての設定をサポートしているわけではありません。例えば `max_turns` は設定できません。高度なユースケースでは、ツール実装内で `Runner.run` を直接使用してください:

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

場合によっては、中央の エージェント に返す前に、ツール化した エージェント の出力を変更したいことがあります。これは次のような場合に有用です:

- サブエージェント のチャット履歴から特定の情報（例: JSON ペイロード）を抽出する。
- エージェント の最終回答を変換または再フォーマットする（例: Markdown をプレーンテキストや CSV に変換）。
- 出力を検証し、エージェント の応答が欠落している、または不正な形式の場合にフォールバック値を提供する。

これは `as_tool` メソッドに `custom_output_extractor` 引数を渡すことで実現できます:

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

### 条件付きツール有効化

`is_enabled` パラメーター を使用して、実行時に エージェント のツールを条件付きで有効化・無効化できます。これにより、コンテキスト、ユーザー の嗜好、実行時条件に基づいて、LLM に提供されるツールを動的にフィルタリングできます。

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = RunContextWrapper(LanguageContext(language_preference="french_spanish"))
    result = await Runner.run(orchestrator, "How are you?", context=context.context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` パラメーター は次を受け付けます:

-  **Boolean values**: `True`（常に有効）または `False`（常に無効）
-  **Callable functions**: `(context, agent)` を取り、真偽値を返す関数
-  **Async functions**: 複雑な条件ロジック向けの非同期関数

無効化されたツールは実行時に LLM から完全に隠されるため、次の用途に役立ちます:

- ユーザー 権限に基づく機能制御
- 環境ごとのツール可用性（dev と prod の切り替え）
- 異なるツール構成の A/B テスト
- 実行時状態に基づく動的なツールフィルタリング

## 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へ返すエラー応答を提供する関数です。

-  既定（何も渡さない場合）では、エラーが発生したことを LLM に伝える `default_tool_error_function` が実行されます。
-  独自のエラー関数を渡した場合はそれが実行され、その応答が LLM に送信されます。
-  明示的に `None` を渡した場合、ツール呼び出しエラーは再送出され、呼び出し側で処理する必要があります。モデルが不正な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などになり得ます。

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

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。