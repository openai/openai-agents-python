---
search:
  exclude: true
---
# エージェント

エージェント はアプリの中核となる基本コンポーネントです。エージェント は、指示とツールで構成された大規模言語モデル（ LLM ）です。

## 基本設定

よく設定するエージェント のプロパティは次のとおりです:

-   `name`: エージェント を識別する必須の文字列です。
-   `instructions`: developer message または system prompt とも呼ばれます。
-   `model`: 使用する LLM と、temperature、top_p などのモデル調整パラメーターを設定する任意の `model_settings`。
-   `tools`: エージェント がタスクを達成するために使用できるツールです。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
     """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## コンテキスト

エージェント はその `context` 型に対して汎用的です。コンテキストは依存性注入ツールです。あなたが作成して `Runner.run()` に渡すオブジェクトで、すべてのエージェント、ツール、ハンドオフ などに渡され、エージェント 実行のための依存関係と状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして提供できます。

```python
@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 出力タイプ

デフォルトでは、エージェント はプレーンテキスト（すなわち `str`）の出力を生成します。特定のタイプの出力をエージェント に生成させたい場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な任意の型（dataclasses、lists、TypedDict など）をサポートします。

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    `output_type` を渡すと、モデルに通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示します。

## ハンドオフ

ハンドオフ は、エージェント が委任できるサブエージェントです。ハンドオフ のリストを提供すると、関連する場合にエージェント はそれらに委任できます。これは、単一のタスクに特化したモジュール型のエージェント をオーケストレーションする強力なパターンです。詳しくは [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions."
        "If they ask about booking, handoff to the booking agent."
        "If they ask about refunds, handoff to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 動的 instructions

多くの場合、エージェント を作成するときに instructions を指定できます。しかし、関数を介して動的な instructions を提供することもできます。この関数はエージェント とコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が使用できます。

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## ライフサイクルイベント（フック）

場合によっては、エージェント のライフサイクルを観測したいことがあります。たとえば、イベントをログに記録したり、特定のイベントが発生した際にデータを事前取得したりする場合です。`hooks` プロパティを使ってエージェント のライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレール により、エージェント の実行と並行して ユーザー 入力に対するチェック／検証を行い、エージェント の出力が生成された際にもチェックできます。たとえば、 ユーザー の入力とエージェント の出力を関連性でスクリーニングできます。詳しくは [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェント の `clone()` メソッドを使用すると、エージェント を複製し、任意で任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="o3-mini",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールのリストを提供しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです:

1. `auto`: LLM がツールを使用するかどうかを判断します。
2. `required`: LLM にツールの使用を要求します（どのツールを使うかは賢く判断できます）。
3. `none`: LLM にツールを使用しないことを要求します。
4. 特定の文字列（例: `my_tool`）を設定すると、LLM にその特定のツールの使用を要求します。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    model_settings=ModelSettings(tool_choice="get_weather") 
)
```

## ツール使用の動作

`Agent` 構成の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します:
- `"run_llm_again"`: デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を最終応答として使用し、以降の LLM 処理は行いません。

```python
from agents import Agent, Runner, function_tool, ModelSettings

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior="stop_on_first_tool"
)
```

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼び出されたら停止し、その出力を最終応答として使用します。
```python
from agents import Agent, Runner, function_tool
from agents.agent import StopAtTools

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b

agent = Agent(
    name="Stop At Stock Agent",
    instructions="Get weather or sum numbers.",
    tools=[get_weather, sum_numbers],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["get_weather"])
)
```
- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を続行するかを判断するカスタム関数です。

```python
from agents import Agent, Runner, function_tool, FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
from typing import List, Any

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

def custom_tool_handler(
    context: RunContextWrapper[Any],
    tool_results: List[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """Processes tool results to decide final output."""
    for result in tool_results:
        if result.output and "sunny" in result.output:
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=f"Final weather: {result.output}"
            )
    return ToolsToFinalOutputResult(
        is_final_output=False,
        final_output=None
    )

agent = Agent(
    name="Weather Agent",
    instructions="Retrieve weather details.",
    tools=[get_weather],
    tool_use_behavior=custom_tool_handler
)
```

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動で "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送られ、`tool_choice` により LLM がさらに別のツール呼び出しを生成し続けるために発生します。