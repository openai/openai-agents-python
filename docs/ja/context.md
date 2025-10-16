---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。考慮すべきコンテキストには主に 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性のあるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどにはラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も ** 重要 ** な点: 特定のエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ _ 型 _ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行のための状況データ（例: ユーザー名/uid などの ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツールの実装はコンテキストから読み取ります。
3. エージェントにジェネリクス `UserInfo` を付与し、型チェッカーがエラーを検知できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストが渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw な引数文字列など）にアクセスしたいことがあります。  
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

`ToolContext` は `RunContextWrapper` と同様に `.context` プロパティを提供し、  
加えて現在のツール呼び出しに特化したフィールドを提供します:

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しを一意に識別する ID  
- `tool_arguments` – ツールに渡された raw な引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間で一般的なコンテキストを共有する場合は、`RunContextWrapper` で十分です。

---

## エージェント/LLM のコンテキスト

LLM が呼び出されると、参照できるのは会話履歴のデータのみです。つまり、LLM に新しいデータを提供したい場合は、その履歴で参照可能になるように提供する必要があります。方法はいくつかあります:

1. Agent の `instructions` に追加します。これは「system prompt（システムプロンプト）」または「developer message」とも呼ばれます。システムプロンプトは固定文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー の名前や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に位置するメッセージを持たせることができます。
3. 関数ツール を通じて公開します。これはオンデマンドのコンテキストに有用で、LLM が必要なタイミングを判断し、ツールを呼び出してデータを取得できます。
4. リトリーバルや Web 検索 を使用します。これらは、ファイルやデータベース（リトリーバル）や Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。