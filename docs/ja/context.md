---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべきコンテキストには、主に 2 つの種類があります:

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック内、ライフサイクルフック内などで必要になる可能性のあるデータや依存関係です。
2. LLM が利用できるコンテキスト: これは、LLM が応答を生成するときに参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラス、およびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass や Pydantic オブジェクトを使用することです。
2. そのオブジェクトを各種実行メソッドに渡します (例: `Runner.run(..., context=whatever)`)。
3. すべてのツール呼び出し、ライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` を通じてアクセスできます。

一部のランタイム固有のコールバックでは、SDK はより特殊化された `RunContextWrapper[T]` のサブクラスを渡す場合があります。たとえば、関数ツールのライフサイクルフックは通常 `ToolContext` を受け取り、これは `tool_call_id`、`tool_name`、`tool_arguments` などのツール呼び出しメタデータも公開します。

認識すべき **最も重要な** 点は、あるエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどが、同じ _型_ のコンテキストを使用しなければならないということです。

コンテキストは、たとえば次の用途に使用できます:

-   実行時のコンテキストデータ (例: ユーザー名 / uid や、ユーザーに関するその他の情報)
-   依存関係 (例: ロガーオブジェクト、データ取得器など)
-   ヘルパー関数

!!! danger "注記"

    コンテキストオブジェクトは LLM に **送信されません**。これは完全にローカルなオブジェクトであり、読み取り、書き込み、メソッドの呼び出しができます。

1 回の実行内では、派生したラッパーは同じ基盤となるアプリコンテキスト、承認状態、使用状況の追跡を共有します。ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行では異なる `tool_input` を付加する場合がありますが、デフォルトではアプリ状態の分離コピーは取得しません。

### `RunContextWrapper` の公開内容

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、アプリで定義したコンテキストオブジェクトを包むラッパーです。実際には、ほとんどの場合、次のものを使用します:

-   [`wrapper.context`][agents.run_context.RunContextWrapper.context]: 独自の可変なアプリ状態と依存関係に使用します。
-   [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]: 現在の実行全体で集計されたリクエストおよびトークン使用量に使用します。
-   [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]: 現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] の内部で実行されている場合の構造化入力に使用します。
-   [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]: 承認状態をプログラムで更新する必要がある場合に使用します。

アプリで定義したオブジェクトは `wrapper.context` のみです。その他のフィールドは SDK が管理するランタイムメタデータです。

後で human-in-the-loop や耐久ジョブワークフローのために [`RunState`][agents.run_state.RunState] をシリアライズする場合、そのランタイムメタデータは状態とともに保存されます。シリアライズされた状態を永続化または送信する予定がある場合、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] にシークレットを入れないでください。

会話状態は別の関心事です。ターンをどのように引き継ぐかに応じて、`result.to_input_list()`、`session`、`conversation_id`、または `previous_response_id` を使用してください。その判断については、[実行結果](results.md)、[エージェントの実行](running_agents.md)、[セッション](sessions/index.md) を参照してください。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることがわかります。ツール実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリック `UserInfo` を指定します (たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合)。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

---

### 高度な内容: `ToolContext`

場合によっては、実行中のツールに関する追加メタデータ (名前、呼び出し ID、生の引数文字列など) にアクセスしたいことがあります。  
この場合、`RunContextWrapper` を拡張する [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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
現在のツール呼び出しに固有の追加フィールドも提供します:

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意の識別子  
- `tool_arguments` – ツールに渡された生の引数文字列  
- `tool_namespace` – ツールが `tool_namespace()` または別の名前空間付きサーフェスを通じて読み込まれた場合の、ツール呼び出しに対する Responses 名前空間  
- `qualified_tool_name` – 名前空間が利用できる場合に、その名前空間で修飾されたツール名  

実行中にツールレベルのメタデータが必要な場合は、`ToolContext` を使用してください。  
エージェントとツール間で一般的なコンテキスト共有を行うには、`RunContextWrapper` のままで十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストされた `Agent.as_tool()` 実行が構造化入力を提供した場合には `.tool_input` も公開できます。

---

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、その LLM が参照できる **唯一の** データは会話履歴に含まれるものです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴内で利用可能になるような方法で行う必要があります。これにはいくつかの方法があります:

1. エージェントの `instructions` に追加できます。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的文字列にも、コンテキストを受け取って文字列を出力する動的関数にもできます。これは、常に役立つ情報 (たとえば、ユーザーの名前や現在の日付) に対する一般的な手法です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージにできます。
3. 関数ツールを介して公開します。これは _オンデマンド_ のコンテキストに便利です。LLM がデータを必要とするタイミングを判断し、そのデータを取得するためにツールを呼び出せます。
4. リトリーバルまたは Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得する (リトリーバル)、または Web から取得する (Web 検索) ことができる特殊なツールです。これは、関連するコンテキストデータに基づいて応答を「グラウンディング」するのに便利です。