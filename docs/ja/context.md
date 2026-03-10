---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。主に重要になるコンテキストは 2 つあります。

1. コード内でローカルに利用可能なコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック時、ライフサイクルフック内などで必要になる可能性があるデータや依存関係です。
2. LLM で利用可能なコンテキスト: これは、レスポンス生成時に LLM が参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass または Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種 run メソッドに渡します (例: `Runner.run(..., context=whatever)`)。
3. すべてのツール呼び出し、ライフサイクルフックなどに、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型であり、`wrapper.context` でアクセスできます。

注意すべき **最も重要な** 点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じコンテキストの _型_ を使用する必要があります。

コンテキストは次のような用途で使えます。

-   実行時の文脈データ (例: ユーザー名 / uid やその他のユーザー情報)
-   依存関係 (例: logger オブジェクト、データフェッチャーなど)
-   ヘルパー関数

!!! danger "注記"

    コンテキストオブジェクトは LLM に送信され **ません**。これは純粋にローカルオブジェクトであり、読み取り、書き込み、メソッド呼び出しを行えます。

単一の実行内では、派生ラッパーは同じ基盤の app コンテキスト、承認状態、使用量トラッキングを共有します。ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行では別の `tool_input` が付与される場合がありますが、既定では app 状態の分離コピーは取得しません。

### `RunContextWrapper` の公開内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、app で定義したコンテキストオブジェクトのラッパーです。実際には、主に次を使用します。

-   独自の可変 app 状態および依存関係には [`wrapper.context`][agents.run_context.RunContextWrapper.context]。
-   現在の実行全体で集計されたリクエストおよびトークン使用量には [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]。
-   現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内で動作している場合の構造化入力には [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]。
-   承認状態をプログラムから更新する必要がある場合は [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]。

`wrapper.context` のみが app で定義したオブジェクトです。その他のフィールドは SDK が管理する実行時メタデータです。

後で human-in-the-loop や永続ジョブワークフローのために [`RunState`][agents.run_state.RunState] をシリアライズする場合、その実行時メタデータは状態とともに保存されます。シリアライズした状態を永続化または送信する予定がある場合、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に秘密情報を入れるのは避けてください。

会話状態は別の関心事項です。ターンをどのように引き継ぐかに応じて、`result.to_input_list()`、`session`、`conversation_id`、または `previous_response_id` を使用してください。この判断については [results](results.md)、[running agents](running_agents.md)、[sessions](sessions/index.md) を参照してください。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使用できます。
2. これがツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストを読み取ります。
3. エージェントにジェネリック `UserInfo` を指定しているため、型チェッカーがエラーを検出できます (たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合)。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

---

### 高度な利用: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ (名前、呼び出し ID、raw 引数文字列など) にアクセスしたいことがあります。  
このために、`RunContextWrapper` を拡張した [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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

`ToolContext` は `RunContextWrapper` と同じ `.context` プロパティを提供し、  
さらに現在のツール呼び出しに固有の追加フィールドを提供します。

- `tool_name` – 呼び出されるツール名  
- `tool_call_id` – このツール呼び出しの一意識別子  
- `tool_arguments` – ツールに渡される raw 引数字符串  
- `tool_namespace` – ツールが `tool_namespace()` または他の名前空間付きサーフェス経由で読み込まれた場合の、ツール呼び出し用 Responses 名前空間  
- `qualified_tool_name` – 名前空間が利用可能な場合の、名前空間付きツール名  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間での一般的なコンテキスト共有では、`RunContextWrapper` で十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストされた `Agent.as_tool()` 実行で構造化入力が渡された場合は `.tool_input` も公開できます。

---

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、参照できるデータは会話履歴内のもの **のみ** です。これは、LLM に新しいデータを利用可能にしたい場合、それを会話履歴内で利用可能にする方法で渡す必要があることを意味します。方法はいくつかあります。

1. Agent の `instructions` に追加できます。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的文字列にも、コンテキストを受け取って文字列を返す動的関数にもできます。これは、常に有用な情報 (たとえばユーザー名や現在日付) に対する一般的な手法です。
2. `Runner.run` 関数を呼び出すときの `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でより下位のメッセージを持てます。
3. 関数ツールを通じて公開します。これは _オンデマンド_ のコンテキストに有用です。つまり、LLM がデータを必要とするタイミングを判断し、そのデータ取得のためにツールを呼び出せます。
4. retrieval または Web 検索を使用します。これらは、ファイルやデータベース (retrieval)、または Web (Web 検索) から関連データを取得できる特別なツールです。これは、関連する文脈データに基づいてレスポンスを「グラウンディング」するのに有用です。