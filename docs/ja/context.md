---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。気にするべきコンテキストには主に 2 つの種類があります。

1. コード側でローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になり得るデータや依存関係です。
2. LLM が利用できるコンテキスト: LLM が応答を生成する際に目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは、dataclass または Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., context=whatever)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

**最も重要**な注意点は、あるエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどが、同じコンテキストの _型_ を使わなければならないことです。

コンテキストは次のような用途に使えます。

-   実行に関するコンテキストデータ（例: ユーザー名 / uid や、ユーザーに関するその他の情報など）
-   依存関係（例: logger オブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信され **ません**。これは純粋にローカルなオブジェクトであり、読み取り、書き込み、メソッド呼び出しができます。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることが分かります。ツール実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントをジェネリック `UserInfo` としてマークします（例: 異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、call ID、raw 引数文字列など）にアクセスしたいことがあります。  
その場合は、`RunContextWrapper` を拡張する [`ToolContext`][agents.tool_context.ToolContext] クラスを使えます。

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from agents.tool_context import ToolContext

class WeatherContext(BaseModel):
    user_id: str

class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")

@function_tool
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` は `RunContextWrapper` と同じ `.context` プロパティに加えて、  
現在のツール呼び出しに固有の追加フィールドを提供します。

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しの一意識別子  
- `tool_arguments` – ツールに渡された raw 引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間での一般的なコンテキスト共有には、`RunContextWrapper` で十分です。

---

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が見られるデータは会話履歴のもの **だけ** です。つまり、新しいデータを LLM から利用できるようにしたい場合は、それが履歴内で利用可能になる方法で行う必要があります。方法はいくつかあります。

1. Agent の `instructions` に追加します。これは「system prompt」や「developer message」とも呼ばれます。システムプロンプトは静的な文字列にすることも、コンテキストを受け取って文字列を出力する動的な関数にすることもできます。これは、常に有用な情報（例: ユーザーの名前や現在日付など）に対してよく使われる戦術です。
2. `Runner.run` 関数を呼び出す際に `input` に追加します。これは `instructions` の戦術に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の中でより下位のメッセージを持てます。
3. 関数ツールとして公開します。これは _オンデマンド_ なコンテキストに有用です。つまり、LLM がいつデータが必要かを判断し、そのデータを取得するためにツールを呼び出せます。
4. 検索や Web 検索を使います。これらは、ファイルやデータベース（検索）や Web（Web 検索）から関連データを取得できる特殊なツールです。これは、関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。