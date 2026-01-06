---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。主に次の 2 つのコンテキストがあります。

1. ローカルでコードから利用できるコンテキスト: これはツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。動作は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**))`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はあなたのコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最 **重要** の注意点: あるエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関する状況データ（例: ユーザー名/UID などのユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得コンポーネントなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しができます。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリクス `UserInfo` を付与しています（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw 引数文字列など）にアクセスしたい場合があります。  
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
現在のツール呼び出しに固有の追加フィールドを提供します。

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意な識別子  
- `tool_arguments` – ツールに渡された raw 引数文字列  

実行時にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間で一般的にコンテキストを共有する用途には、`RunContextWrapper` で十分です。

---

## エージェント / LLM コンテキスト

LLM が呼び出されると、LLM が参照できる **唯一** のデータは会話履歴です。つまり、LLM に新しいデータを利用させたい場合は、その履歴で参照できるようにする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「system prompt」や「developer message」とも呼ばれます。システムプロンプトは固定の文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に有用な情報（例: ユーザーの名前や現在の日付）には一般的な手法です。
2. `Runner.run` 関数を呼び出すときの `input` に追加します。これは `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. function tools を通じて公開します。これはオンデマンドのコンテキストに有用です。LLM が必要に応じてデータを要求し、ツールを呼び出してそのデータを取得できます。
4. retrieval や Web 検索を使います。これらはファイルやデータベース（retrieval）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに応答を「グラウンディング」するのに有用です。