---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語には複数の意味があります。主に考慮すべきコンテキストには、次の 2 種類があります。

1. コードがローカルで利用できるコンテキスト：ツール関数の実行時、`on_handoff` のようなコールバック内、ライフサイクルフック内などで必要になる可能性のあるデータや依存関係です。
2. LLM が利用できるコンテキスト：レスポンスの生成時に LLM が参照するデータです。

## ローカルコンテキスト {#local-context}

これは、[`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは、dataclass または Pydantic オブジェクトを使用することです。
2. そのオブジェクトを各種実行メソッド（例：`Runner.run(..., context=whatever)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで、`T` はコンテキストオブジェクトの型を表し、オブジェクト自体には `wrapper.context` からアクセスできます。

ランタイム固有の一部のコールバックでは、SDK が `RunContextWrapper[T]` のより特化したサブクラスを渡す場合があります。たとえば、`FunctionTool` インスタンスのライフサイクルフックは通常 `ToolContext` を受け取り、`tool_call_id`、`tool_name`、`tool_arguments` などのツール呼び出しメタデータにもアクセスできます。

認識すべき **最も重要な** 点は、特定のエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどが、同じ _型_ のコンテキストを使用する必要があることです。

コンテキストは、次のような用途に使用できます。

-   実行に関するコンテキストデータ（ユーザー名 / uid や、ユーザーに関するその他の情報など）
-   依存関係（ロガーオブジェクトやデータフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません** 。これは純粋にローカルなオブジェクトであり、値の読み取りや書き込み、メソッドの呼び出しが可能です。

1 回の実行内では、派生したラッパーが同じ基盤のアプリケーションコンテキスト、承認状態、使用量追跡を共有します。ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行では、別の `tool_input` が関連付けられる場合がありますが、デフォルトではアプリケーション状態の分離されたコピーは作成されません。

### ローカルコンテキストによる機能の可視性制御 {#use-local-context-for-capability-visibility}

関数ツール、MCP ツール、ハンドオフが同じリクエストポリシーに依存する場合は、ポリシーの入力またはヘルパーをアプリケーションコンテキストに保持してください。SDK の各機能は、それぞれのコールバックを通じて現在の実行コンテキストを公開します。

-   [`FunctionTool.is_enabled`][agents.tool.FunctionTool.is_enabled] は `RunContextWrapper` を受け取ります。
-   [`Handoff.is_enabled`][agents.handoffs.Handoff.is_enabled] は `RunContextWrapper` を受け取ります。
-   MCP の [`tool_filter`](mcp.md#dynamic-tool-filtering) は [`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取ります。その `run_context` プロパティには、現在の `RunContextWrapper` が含まれます。

機能リストを個別に管理するのではなく、共有のアプリケーションポリシーをこれらのコールバックに適合させてください。コールバックは、SDK が現在の実行で公開する機能を制御しますが、モデルが生成した引数やリソースの選択を承認することはできません。関数ツールでは、ツールの実装内で、または必要に応じて[ツール入力ガードレール](guardrails.md#tool-guardrails)や[承認](human_in_the_loop.md)を使用して、それらの判断を適用してください。MCP サーバーは、保護された操作を自身で承認する必要があります。`input_type` を持つハンドオフでは、アプリケーション側の副作用が発生する前に、`on_handoff` の冒頭で解析済みの入力を確認し、承認に失敗した場合は値を返すのではなく例外を送出してください。ツール入力ガードレールはハンドオフでは実行されません。コールバックのライフサイクルについては、[ハンドオフ入力](handoffs.md#handoff-inputs)を参照してください。

### `RunContextWrapper` が公開する情報 {#what-runcontextwrapper-exposes}

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、アプリケーションで定義したコンテキストオブジェクトのラッパーです。実際には、主に次のものを使用します。

-   [`wrapper.context`][agents.run_context.RunContextWrapper.context]：独自の変更可能なアプリケーション状態と依存関係。
-   [`wrapper.usage`][agents.run_context.RunContextWrapper.usage]：現在の実行全体で集計されたリクエストとトークンの使用量。
-   [`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input]：現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内で行われている場合の構造化入力。
-   [`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool]：プログラムから承認状態を更新する必要がある場合に使用します。

アプリケーションで定義したオブジェクトは `wrapper.context` だけです。その他のフィールドは、SDK が管理するランタイムメタデータです。

後で human-in-the-loop または永続ジョブのワークフローのために [`RunState`][agents.run_state.RunState] をシリアライズすると、そのランタイムメタデータも状態とともに保存されます。シリアライズした状態を永続化または送信する予定がある場合は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] にシークレットを保存しないでください。

会話状態は別の考慮事項です。会話ターンをどのように引き継ぐかに応じて、`result.to_input_list()`、`session`、`conversation_id`、または `previous_response_id` を使用してください。この判断については、[実行結果](results.md)、[エージェントの実行](running_agents.md)、[セッション](sessions/index.md)を参照してください。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner
from agents.decorators import tool


@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int


@tool
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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることが分かります。ツールの実装はコンテキストから値を読み取ります。
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 高度な機能：`ToolContext` {#advanced-toolcontext}

場合によっては、実行中のツールに関する追加のメタデータ（名前、呼び出し ID、生の引数文字列など）にアクセスしたいことがあります。  
この場合は、`RunContextWrapper` を拡張する [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

```python
from typing import Annotated
from pydantic import BaseModel, Field
from agents import Agent
from agents.decorators import tool
from agents.tool_context import ToolContext


class WeatherContext(BaseModel):
    user_id: str


class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")


@tool
def get_weather(
    ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]
) -> Weather:
    print(
        f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})"
    )
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` は、`RunContextWrapper` と同じ `.context` プロパティを提供し、  
さらに現在のツール呼び出しに固有の次のフィールドも提供します。

- `tool_name` – 呼び出されているツールの名前  
- `tool_call_id` – このツール呼び出しの一意な識別子  
- `tool_arguments` – ツールに渡された生の引数文字列  
- `tool_namespace` – ツールが `tool_namespace()` または名前空間を持つ別の機能を通じて読み込まれた場合の、ツール呼び出しの Responses 名前空間  
- `qualified_tool_name` – 名前空間が利用可能な場合に、その名前空間で修飾されたツール名  

実行中にツールレベルのメタデータが必要な場合は、`ToolContext` を使用してください。  
エージェントとツール間で一般的なコンテキストを共有する場合は、引き続き `RunContextWrapper` で十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストされた `Agent.as_tool()` の実行から構造化入力が渡された場合は、`.tool_input` にもアクセスできます。

---

## エージェント / LLM コンテキスト {#agentllm-context}

LLM が呼び出されたとき、LLM が参照できるデータは会話履歴に含まれるもの **だけ** です。つまり、LLM が新しいデータを利用できるようにするには、そのデータを会話履歴に含める必要があります。これには、いくつかの方法があります。

1. エージェントの `instructions` に追加できます。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトには静的な文字列を指定できるほか、コンテキストを受け取って文字列を出力する動的な関数も指定できます。これは、常に有用な情報（ユーザー名や現在の日付など）に対してよく使われる方法です。
2. `Runner.run` 関数を呼び出すときに、`input` に追加します。これは `instructions` を使用する方法と似ていますが、[指示の優先順位](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)において優先度の低いメッセージを使用できます。
3. `FunctionTool` インスタンスを通じて公開します。これは _オンデマンド_ コンテキストに有用です。LLM がデータを必要とするタイミングを判断し、ツールを呼び出してそのデータを取得できます。
4. 検索（retrieval）または Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得する（retrieval）、または Web から取得する（Web 検索）ための特別なツールです。これは、関連するコンテキストデータに基づいてレスポンスを「根拠付け」する場合に有用です。