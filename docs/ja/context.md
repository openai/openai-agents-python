---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。主に、重要になるコンテキストには 2 つの分類があります。

1. コード内でローカルに利用可能なコンテキスト: これは、関数ツールの実行時、`on_handoff` のようなコールバック時、ライフサイクルフック時などに必要になる可能性があるデータや依存関係です。
2. LLM が利用可能なコンテキスト: これは、LLM がレスポンスを生成するときに参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは、dataclass または Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., context=whatever)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどには `RunContextWrapper[T]` のラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

ランタイム固有の一部コールバックでは、SDK が `RunContextWrapper[T]` のより特化したサブクラスを渡す場合があります。たとえば、関数ツールのライフサイクルフックは通常 `ToolContext` を受け取り、`tool_call_id`、`tool_name`、`tool_arguments` などのツール呼び出しメタデータにもアクセスできます。

認識しておくべき **最も重要** な点: 特定のエージェント実行におけるすべてのエージェント、関数ツール、ライフサイクルなどは、同じコンテキストの _型_ を使用する必要があります。

コンテキストは次のような用途で使用できます。

-   実行のためのコンテキストデータ（例: ユーザー名 / uid や、ユーザーに関するその他の情報）
-   依存関係（例: logger オブジェクト、データ取得処理など）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルオブジェクトであり、読み取り、書き込み、メソッド呼び出しが可能です。

1 回の実行内では、派生ラッパーは同じ基盤のアプリコンテキスト、承認状態、使用量トラッキングを共有します。ネストした [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行では別の `tool_input` が付与される場合がありますが、デフォルトではアプリ状態の分離コピーは取得しません。

### `RunContextWrapper` の公開内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、アプリで定義したコンテキストオブジェクトのラッパーです。実際には、主に次を使用します。

-   独自の可変アプリ状態および依存関係には [`wrapper.context`][agents.run_context.RunContextWrapper.context]。
-   現在の実行全体の集計されたリクエストおよびトークン使用量には [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]。
-   現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内で実行されているときの構造化入力には [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]。
-   承認状態をプログラムで更新する必要がある場合は [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]。

アプリで定義したオブジェクトは `wrapper.context` のみです。その他のフィールドは SDK が管理するランタイムメタデータです。

後で human-in-the-loop や永続ジョブワークフロー向けに [`RunState`][agents.run_state.RunState] をシリアライズする場合、そのランタイムメタデータは状態とともに保存されます。シリアライズした状態を永続化または送信する予定がある場合は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] にシークレットを入れないでください。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントをジェネリック `UserInfo` で指定します（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

---

### 高度な使用法: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、呼び出し ID、生の引数文字列など）にアクセスしたいことがあります。  
このために、`RunContextWrapper` を拡張する [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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
さらに現在のツール呼び出しに固有の追加フィールドも提供します。

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しの一意識別子  
- `tool_arguments` – ツールに渡される生の引数文字列  
- `tool_namespace` – ツールが `tool_namespace()` または他の名前空間付きサーフェスを通じて読み込まれた場合の、ツール呼び出しの Responses 名前空間  
- `qualified_tool_name` – 名前空間が利用可能な場合に、その名前空間で修飾されたツール名  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間の一般的なコンテキスト共有には、`RunContextWrapper` で十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストした `Agent.as_tool()` 実行が構造化入力を提供した場合は `.tool_input` も公開できます。

---

## エージェント / LLM コンテキスト

LLM が呼び出されると、参照できるデータは会話履歴にあるもの **のみ** です。つまり、新しいデータを LLM で利用可能にしたい場合は、その履歴で利用できる形にする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的文字列にもできますし、コンテキストを受け取って文字列を返す動的関数にもできます。これは、常に有用な情報（たとえばユーザー名や現在日付）に対する一般的な手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でより下位のメッセージを持てます。
3. 関数ツールを介して公開します。これは _オンデマンド_ のコンテキストに有用です。LLM がデータを必要とするタイミングを判断し、そのデータを取得するためにツールを呼び出せます。
4. retrieval または Web 検索を使用します。これらは、ファイルやデータベース（retrieval）、または Web（Web 検索）から関連データを取得できる特別なツールです。これは、レスポンスを関連するコンテキストデータに「グラウンディング」するのに有用です。