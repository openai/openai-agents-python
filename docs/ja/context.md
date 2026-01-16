---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。ここでは主に 2 つのコンテキストがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答を生成するときに LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで T はあなたのコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最重要** な点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じ型のコンテキストを使わなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関する状況データ（例: ユーザー名/uid やユーザーに関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM には送信されません。あくまでローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装はコンテキストから読み取ります。
3. エージェントにジェネリクス `UserInfo` を指定して、型チェッカーがエラーを検出できるようにします（たとえば異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw の引数文字列など）へアクセスしたい場合があります。  
その場合は、`RunContextWrapper` を拡張した [`ToolContext`][agents.tool_context.ToolContext] クラスを使えます。

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

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しの一意な識別子  
- `tool_arguments` – ツールに渡された raw の引数文字列  

実行中にツールレベルのメタデータが必要なときは `ToolContext` を使ってください。  
エージェントとツール間で一般的なコンテキスト共有を行うだけであれば、`RunContextWrapper` で十分です。

---

## エージェント / LLM コンテキスト

LLM が呼び出されると、そのときに見えるデータは会話履歴のみです。つまり、新しいデータを LLM で利用可能にするには、その履歴で参照できるようにしなければなりません。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「system prompt」または「developer message」とも呼ばれます。system prompts は静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザー名や現在の日付）に適した手法です。
2. `Runner.run` 関数を呼び出すときの `input` に追加します。これは `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) において下位のメッセージを用意できます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに適しており、LLM が必要に応じてツールを呼び出し、そのデータを取得できます。
4. リトリーバルまたは Web 検索を使います。これらは、ファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。