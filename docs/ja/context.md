---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。重視すべきコンテキストには、主に次の 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは、応答を生成するときに LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の概要は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

 **最も重要** な注意点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じ「型」のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます。

- 実行用のコンテキストデータ（例: ユーザー名/uid など、ユーザーに関する情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトで、読み取り・書き込みやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装ではコンテキストから読み取っています。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリックな `UserInfo` を指定します（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 上級: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、raw な引数文字列など）にアクセスしたいことがあります。  
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

`ToolContext` は `RunContextWrapper` と同じ `.context` プロパティに加え、現在のツール呼び出しに固有の次のフィールドを提供します。

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意な識別子  
- `tool_arguments` – ツールに渡された raw な引数文字列  

実行時にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間で一般的にコンテキストを共有するだけであれば、`RunContextWrapper` で十分です。

---

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるのは会話履歴のデータ **のみ** です。つまり、新しいデータを LLM に利用可能にしたい場合は、そのデータが会話履歴に含まれるようにする必要があります。これにはいくつか方法があります。

1. エージェントの `instructions` に追加します。これは "system prompt"（システムプロンプト）または "developer message" とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。これは常に有用な情報（例: ユーザーの名前や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に位置するメッセージとして追加できます。
3. 関数ツール経由で公開します。これはオンデマンドなコンテキストに有用です。LLM が必要なときにデータ取得を判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、応答を関連するコンテキストデータに「グラウンディング」するのに有用です。