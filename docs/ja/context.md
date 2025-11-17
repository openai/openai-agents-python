---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。ここで重要になるコンテキストには主に 2 つのクラスがあります。

1. コードからローカルに利用可能なコンテキスト: これは、ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは、[`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要な点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用しなければなりません。

コンテキストは以下のような用途に使えます:

-   実行のための状況依存データ（例: ユーザー名/uid などユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得ロジックなど）
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

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツールの実装がコンテキストから読み取っています。
3. エージェントをジェネリクスの `UserInfo` でマークし、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 詳細: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（ツール名、呼び出し ID、raw な引数文字列など）にアクセスしたいことがあります。  
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
現在のツール呼び出しに固有の追加フィールドを提供します:

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意の識別子  
- `tool_arguments` – ツールに渡された raw な引数文字列  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間での一般的なコンテキスト共有には、`RunContextWrapper` で十分です。

---

## エージェント/LLM のコンテキスト

LLM が呼び出されると、参照できるのは会話履歴にあるデータのみです。つまり、新しいデータを LLM で利用可能にするには、その履歴で利用できる形で提供する必要があります。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。これは常に有用な情報（例: ユーザーの名前や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)の下位にメッセージを配置できます。
3. 関数ツール経由で公開します。これはオンデマンドのコンテキストに有用で、LLM が必要なときにデータ取得のためにツールを呼び出せます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）や Web（Web 検索）から関連データを取得できる特別なツールです。これは、応答を関連する状況データでグラウンディングするのに有用です。