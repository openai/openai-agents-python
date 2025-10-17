---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。気にすべきコンテキストには主に 2 つのクラスがあります。

1. コードからローカルに参照できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック中、ライフサイクルフックなどで必要になる可能性のあるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは、LLM が応答を生成するときに目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要** な注意点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは、同じ _型_ のコンテキストを使わなければなりません。

コンテキストは次のような用途に使えます:

-   実行に関するコンテキストデータ（例: ユーザー名 / UID やその他のユーザー情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送られません**。これは純粋にローカルなオブジェクトであり、読み取りや書き込み、メソッド呼び出しができます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型が使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリックの `UserInfo` を付与します（たとえば、異なるコンテキスト型を受け取るツールを渡した場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw 引数文字列など）にアクセスしたいことがあります。  
そのために、`RunContextWrapper` を拡張した [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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

`ToolContext` は `RunContextWrapper` と同じ `.context` プロパティに加え、  
現在のツール呼び出しに特化した追加フィールドを提供します:

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意の識別子  
- `tool_arguments` – ツールに渡された raw 引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間で一般的にコンテキストを共有するだけなら、`RunContextWrapper` で十分です。

---

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が見られるデータは会話履歴にあるものだけです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは "system prompt"（開発者メッセージ）とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。これは常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを持てます。
3. 関数ツール経由で公開します。これはオンデマンドのコンテキストに有用です。LLM が必要なときにデータを取得するため、ツールを呼び出せます。
4. リトリーバル（retrieval）や Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（リトリーバル）したり、Web から取得（Web 検索）したりできる特別なツールです。これは、応答を関連するコンテキストデータに「根拠付け」するのに有用です。