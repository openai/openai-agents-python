---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。主に次の 2 つのクラスのコンテキストがあります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバックやライフサイクルフックなどで必要になる可能性のあるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、LLM が応答を生成する際に目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどは、ラッパーオブジェクト `RunContextWrapper[T]` を受け取ります。ここで `T` はあなたのコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

ここで最も **重要** な注意点: あるエージェント実行の中で使われるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ種類（_type_）のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行のためのコンテキストデータ（例: ユーザー名/UID や、ユーザーに関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルのオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることが分かります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリクス `UserInfo` を付与します（例えば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストが渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw 引数文字列など）にアクセスしたいことがあります。  
そのためには、`RunContextWrapper` を拡張した [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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
現在のツール呼び出しに固有の以下のフィールドを提供します。

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意の識別子  
- `tool_arguments` – ツールに渡された raw 引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間で一般的にコンテキストを共有するだけであれば、`RunContextWrapper` で十分です。

---

## エージェント/LLM のコンテキスト

LLM が呼び出されると、LLM が見られるデータは会話履歴のみです。つまり、LLM に新しいデータを使わせたい場合は、その履歴で利用可能になる方法で渡す必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは "system prompt"（または "developer message"）としても知られています。system prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。これは常に有用な情報（例: ユーザーの名前や現在の日付）に一般的な手法です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを持たせることができます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに有用です。LLM が必要なときにそのデータを判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータに基づいて応答をグラウンディングするのに有用です。