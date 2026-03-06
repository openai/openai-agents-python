---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。主に、関心を持つ可能性があるコンテキストには 2 つのクラスがあります。

1. コードでローカルに利用可能なコンテキスト: これは、関数ツールの実行時、`on_handoff` のようなコールバック時、ライフサイクルフック時などに必要になる可能性があるデータや依存関係です。
2. LLM から利用可能なコンテキスト: これは、LLM がレスポンス生成時に参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass または Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., context=whatever)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

認識しておくべき **最も重要** な点: あるエージェント実行に対するすべてのエージェント、関数ツール、ライフサイクルなどは、同じコンテキストの _型_ を使う必要があります。

コンテキストは次のような用途に使えます。

-   実行のためのコンテキストデータ（例: username / uid やユーザーに関するその他情報）
-   依存関係（例: logger オブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルオブジェクトであり、読み取り、書き込み、メソッド呼び出しを行えます。

単一の実行内では、派生ラッパーは同じ基盤の app context、承認状態、使用量トラッキングを共有します。ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行では別の `tool_input` が付与される場合がありますが、デフォルトでは app 状態の分離コピーは取得しません。

### `RunContextWrapper` の公開内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、アプリで定義したコンテキストオブジェクトのラッパーです。実際には、最もよく使うのは次です。

-   独自の可変 app 状態と依存関係のための [`wrapper.context`][agents.run_context.RunContextWrapper.context]。
-   現在の実行全体で集計されたリクエストとトークン使用量のための [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]。
-   現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内で動いているときの構造化入力のための [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]。
-   承認状態をプログラムで更新する必要があるときの [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]。

`wrapper.context` だけがアプリ定義のオブジェクトです。他のフィールドは SDK が管理するランタイムメタデータです。

後で human-in-the-loop や耐久ジョブワークフロー向けに [`RunState`][agents.run_state.RunState] をシリアライズする場合、そのランタイムメタデータは状態とともに保存されます。シリアライズ状態を永続化または送信する予定がある場合は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] に秘密情報を入れないでください。

会話状態は別の関心事項です。ターンをどう引き継ぐかに応じて、`result.to_input_list()`、`session`、`conversation_id`、`previous_response_id` を使い分けてください。この判断については [results](results.md)、[running agents](running_agents.md)、[sessions](sessions/index.md) を参照してください。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストを読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリック `UserInfo` を付けます（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

---

### 発展: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ（名前、call ID、raw 引数文字列など）にアクセスしたいことがあります。  
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

`ToolContext` は `RunContextWrapper` と同じ `.context` プロパティに加えて、  
現在のツール呼び出しに固有の追加フィールドを提供します。

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しの一意識別子  
- `tool_arguments` – ツールに渡された raw 引数字符串  

実行中にツールレベルのメタデータが必要な場合は `ToolContext` を使用してください。  
エージェントとツール間の一般的なコンテキスト共有には、`RunContextWrapper` で十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストされた `Agent.as_tool()` 実行で構造化入力が渡された場合は `.tool_input` も公開できます。

---

## Agent/LLM コンテキスト

LLM が呼び出されるとき、参照できるデータは会話履歴にあるもの **のみ** です。つまり、新しいデータを LLM で利用可能にしたい場合は、その履歴で利用可能になる形で渡す必要があります。方法はいくつかあります。

1. Agent の `instructions` に追加します。これは "システムプロンプト" または "developer message" とも呼ばれます。システムプロンプトは静的な文字列にも、コンテキストを受け取って文字列を出力する動的関数にもできます。これは、常に有用な情報（たとえばユーザー名や現在日付）でよく使われる手法です。
2. `Runner.run` 関数を呼ぶ際に `input` に追加します。これは `instructions` の手法に近いですが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の中でより下位のメッセージを持てます。
3. 関数ツール経由で公開します。これは _オンデマンド_ なコンテキストに有用です。LLM がデータを必要とするタイミングを判断し、そのデータ取得のためにツールを呼び出せます。
4. retrieval または Web 検索を使用します。これらは、ファイルやデータベース（retrieval）または Web（web search）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータでレスポンスを「グラウンディング」するのに有用です。