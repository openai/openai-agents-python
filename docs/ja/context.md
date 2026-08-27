---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は複数の意味で使われます。考慮すべきコンテキストは、大きく次の 2 種類に分けられます。

1. コードがローカルで利用できるコンテキスト：ツール関数の実行時、`on_handoff` などのコールバック内、ライフサイクルフック内などで必要になる可能性があるデータや依存関係です。
2. LLM が利用できるコンテキスト：レスポンスを生成する際に LLM が参照するデータです。

## ローカルコンテキスト {#local-context}

これは、[`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には、データクラスまたは Pydantic オブジェクトを使用します。
2. そのオブジェクトを各種の実行メソッド（例：`Runner.run(..., context=whatever)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、オブジェクト自体には `wrapper.context` を介してアクセスできます。

ランタイム固有の一部のコールバックでは、SDK が `RunContextWrapper[T]` のより特化したサブクラスを渡す場合があります。たとえば、`FunctionTool` インスタンスのライフサイクルフックは通常、`ToolContext` を受け取ります。これは、`tool_call_id`、`tool_name`、`tool_arguments` などのツール呼び出しメタデータも公開します。

注意すべき **最も重要な** 点は、特定のエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクル処理などで、同じ _型_ のコンテキストを使用する必要があることです。

コンテキストは、次のような用途に使用できます。

-   実行に関するコンテキストデータ（例：ユーザー名、UID、その他のユーザー情報）
-   依存関係（例：ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注記"

    コンテキストオブジェクトは、LLM に **送信されない** ローカル専用のオブジェクトです。その値の読み取りや書き込み、メソッドの呼び出しが可能です。

1 回の実行内では、派生したラッパーが同じ基盤のアプリケーションコンテキスト、承認状態、使用量追跡を共有します。ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行では、異なる `tool_input` が付与される場合がありますが、デフォルトではアプリケーション状態の独立したコピーは作成されません。

### 機能の公開制御におけるローカルコンテキストの使用 {#use-local-context-for-capability-visibility}

関数ツール、MCP ツール、ハンドオフが同じリクエストポリシーに依存する場合は、ポリシーの入力値またはヘルパーをアプリケーションコンテキストに保持してください。SDK の各インターフェースは、それぞれのコールバックを介して現在の実行コンテキストを公開します。

-   [`FunctionTool.is_enabled`][agents.tool.FunctionTool.is_enabled] は `RunContextWrapper` を受け取ります。
-   [`Handoff.is_enabled`][agents.handoffs.Handoff.is_enabled] は `RunContextWrapper` を受け取ります。
-   MCP の [`tool_filter`](mcp.md#dynamic-tool-filtering) は [`ToolFilterContext`][agents.mcp.ToolFilterContext] を受け取ります。その `run_context` プロパティには、現在の `RunContextWrapper` が含まれます。

個別の機能リストを管理するのではなく、共有アプリケーションポリシーをこれらのコールバックに合わせて適用してください。これらのコールバックは、現在の実行に対して SDK が公開する機能を制御しますが、モデルが生成した引数やリソース選択を認可することはできません。関数ツールでは、ツール実装内で認可に関する判断を適用するか、必要に応じて[ツール入力ガードレール](guardrails.md#tool-guardrails)や[承認](human_in_the_loop.md)を使用してください。MCP サーバーは、自身の保護対象の操作を認可する必要があります。`input_type` を持つハンドオフでは、アプリケーションに副作用が生じる前に、`on_handoff` の冒頭で解析済みの入力を確認し、認可に失敗した場合は値を返さずに例外を送出してください。ツール入力ガードレールは、ハンドオフでは実行されません。コールバックのライフサイクルについては、[ハンドオフ入力](handoffs.md#handoff-inputs)を参照してください。

### `RunContextWrapper` で公開される情報 {#what-runcontextwrapper-exposes}

[`RunContextWrapper`][agents.run_context.RunContextWrapper] は、アプリケーションで定義したコンテキストオブジェクトのラッパーです。実際には、主に次の項目を使用します。

-   独自の変更可能なアプリケーション状態と依存関係には、[`wrapper.context`][agents.run_context.RunContextWrapper.context] を使用します。
-   現在の実行全体で集計されたリクエストとトークンの使用量には、[`wrapper.usage`][agents.run_context.RunContextWrapper.usage] を使用します。
-   現在の実行が [`Agent.as_tool()`][agents.agent.Agent.as_tool] 内で行われている場合の構造化入力には、[`wrapper.tool_input`][agents.run_context.RunContextWrapper.tool_input] を使用します。
-   承認状態をプログラムで更新する必要がある場合は、[`wrapper.approve_tool(...)`][agents.run_context.RunContextWrapper.approve_tool] / [`wrapper.reject_tool(...)`][agents.run_context.RunContextWrapper.reject_tool] を使用します。

アプリケーションで定義したオブジェクトは `wrapper.context` だけです。その他のフィールドは、SDK が管理するランタイムメタデータです。

後で Human-in-the-loop または永続ジョブのワークフロー向けに [`RunState`][agents.run_state.RunState] をシリアライズする場合、このランタイムメタデータも状態とともに保存されます。シリアライズした状態を永続化または送信する予定がある場合は、[`RunContextWrapper.context`][agents.run_context.RunContextWrapper.context] にシークレットを格納しないでください。

会話状態は別の検討事項です。ターンを引き継ぐ方法に応じて、`result.to_input_list()`、`session`、`conversation_id`、または `previous_response_id` を使用してください。この判断については、[実行結果](results.md)、[エージェントの実行](running_agents.md)、[セッション](sessions/index.md)を参照してください。

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

1. これはコンテキストオブジェクトです。ここではデータクラスを使用していますが、任意の型を使用できます。
2. これはツールです。このツールが `RunContextWrapper[UserInfo]` を受け取ることが分かります。ツール実装はコンテキストから値を読み取ります。
3. エージェントにジェネリック `UserInfo` を指定し、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

---

### 高度な機能： `ToolContext` {#advanced-toolcontext}

場合によっては、実行中のツールに関する追加のメタデータ（名前、呼び出し ID、生の引数文字列など）へアクセスする必要があります。  
その場合は、`RunContextWrapper` を拡張する [`ToolContext`][agents.tool_context.ToolContext] クラスを使用できます。

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
def get_weather(ctx: ToolContext[WeatherContext], city: Annotated[str, "The city to get the weather for"]) -> Weather:
    print(f"[debug] Tool context: (name: {ctx.tool_name}, call_id: {ctx.tool_call_id}, args: {ctx.tool_arguments})")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")

agent = Agent(
    name="Weather Agent",
    instructions="You are a helpful agent that can tell the weather of a given city.",
    tools=[get_weather],
)
```

`ToolContext` は、`RunContextWrapper` と同じ `.context` プロパティを提供し、  
さらに現在のツール呼び出しに固有の次のフィールドも提供します。

- `tool_name` – 呼び出されるツールの名前  
- `tool_call_id` – このツール呼び出しの一意の識別子  
- `tool_arguments` – ツールに渡された生の引数文字列  
- `tool_namespace` – ツールが `tool_namespace()` または名前空間を持つ別のインターフェースを介して読み込まれた場合の、ツール呼び出し用 Responses 名前空間  
- `qualified_tool_name` – 名前空間が利用できる場合に、その名前空間で修飾されたツール名  

実行中にツール単位のメタデータが必要な場合は、`ToolContext` を使用してください。  
エージェントとツール間で一般的なコンテキストを共有する場合は、引き続き `RunContextWrapper` で十分です。`ToolContext` は `RunContextWrapper` を拡張しているため、ネストされた `Agent.as_tool()` の実行で構造化入力が指定された場合は、`.tool_input` も公開できます。

---

## エージェント / LLM コンテキスト {#agentllm-context}

LLM が呼び出されたとき、LLM が確認できる **唯一の** データは会話履歴に含まれるデータです。つまり、新しいデータを LLM が利用できるようにするには、そのデータを会話履歴に含める必要があります。これには、次のような方法があります。

1. エージェントの `instructions` に追加できます。これは「システムプロンプト」または「developer message」とも呼ばれます。システムプロンプトには静的な文字列を指定できるほか、コンテキストを受け取って文字列を出力する動的関数も使用できます。これは、常に有用な情報（たとえば、ユーザー名や現在の日付）に対してよく使われる方法です。
2. `Runner.run` 関数を呼び出す際に、`input` に追加します。これは `instructions` を使用する方法と似ていますが、[指示の優先順位](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)がより低いメッセージを使用できます。
3. `FunctionTool` インスタンスを介して公開します。これは _オンデマンド_ コンテキストに便利です。LLM がデータを必要とするタイミングを判断し、ツールを呼び出してそのデータを取得できます。
4. 検索または Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（検索）したり、Web から取得（Web 検索）したりできる特別なツールです。これは、関連するコンテキストデータに基づいてレスポンスを根拠付ける場合に便利です。