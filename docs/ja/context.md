---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。関心を持つべきコンテキストには主に 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答生成時に LLM が参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の run メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどに、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も **重要** な点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクル等は、同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関するコンテキストデータ（例: ユーザー名/UID や ユーザー に関する他の情報）
-   依存関係（例: ロガーオブジェクト、データ取得器など）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM には送信されません。読み取りや書き込み、メソッド呼び出しができる純粋なローカルオブジェクトです。

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
3. エージェントに総称型 `UserInfo` を付け、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw 引数文字列など）にアクセスしたいことがあります。  
その場合は、`RunContextWrapper` を拡張した [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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
現在のツール呼び出しに特化した追加フィールドを提供します。

- `tool_name` – 呼び出されているツール名  
- `tool_call_id` – このツール呼び出しの一意な識別子  
- `tool_arguments` – ツールに渡された raw 引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使ってください。  
エージェント間やツール間での一般的なコンテキスト共有には、`RunContextWrapper` で十分です。

---

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、参照できるのは会話履歴のデータだけです。つまり、新しいデータを LLM に利用させたい場合は、その履歴で参照可能になるようにデータを提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは固定文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザー名や現在日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) において、より下位のメッセージにできます。
3. 関数ツール を通じて公開します。これはオンデマンドのコンテキストに便利です。LLM が必要に応じてデータ取得のためにツールを呼び出せます。
4. リトリーバルや Web 検索 を使います。これは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答をグラウンディングするのに有用です。