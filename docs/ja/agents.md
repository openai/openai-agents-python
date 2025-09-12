---
search:
  exclude: true
---
# エージェント

エージェント はアプリのコアとなるビルディングブロックです。エージェント は大規模言語モデル（ LLM ）で、 instructions とツールで構成します。

## 基本構成

エージェント で最も一般的に設定するプロパティは次のとおりです。

- `name`: エージェント を識別する必須の文字列です。
- `instructions`: developer メッセージ、または system prompt とも呼ばれます。
- `model`: 使用する LLM と、 temperature、 top_p などのモデル調整パラメーターを設定する任意の `model_settings`。
- `tools`: エージェント がタスクを達成するために使用できるツール。

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

エージェント はその `context` 型に対してジェネリックです。コンテキストは依存性注入ツールです。あなたが作成して `Runner.run()` に渡すオブジェクトで、すべてのエージェント、ツール、ハンドオフ などに渡され、エージェント の実行に必要な依存関係や状態をまとめて保持します。コンテキストには任意の Python オブジェクトを提供できます。

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

デフォルトでは、エージェント はプレーンテキスト（すなわち `str`）を出力します。特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使いますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型（dataclasses、lists、TypedDict など）をサポートします。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するようになります。

## マルチ エージェント システムの設計パターン

マルチ エージェント システムの設計方法は多数ありますが、一般的に広く適用できるパターンを 2 つ挙げます。

1. マネージャー（エージェント をツールとして使用）: 中央のマネージャー/オーケストレーターが、専用のサブ エージェント をツールとして呼び出し、会話の制御を保持します。
2. ハンドオフ: ピア エージェント が制御を、会話を引き継ぐ特化型エージェント にハンドオフします。こちらは分散型です。

詳細は [エージェント 構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) をご覧ください。

### マネージャー（エージェント をツールとして使用）

`customer_facing_agent` がすべてのユーザー とのやり取りを処理し、ツールとして公開された専用のサブ エージェント を呼び出します。詳しくは [ツール](tools.md#agents-as-tools) ドキュメントをご覧ください。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

customer_facing_agent = Agent(
    name="Customer-facing agent",
    instructions=(
        "Handle all direct user communication. "
        "Call the relevant tools when specialized expertise is needed."
    ),
    tools=[
        booking_agent.as_tool(
            tool_name="booking_expert",
            tool_description="Handles booking questions and requests.",
        ),
        refund_agent.as_tool(
            tool_name="refund_expert",
            tool_description="Handles refund questions and requests.",
        )
    ],
)
```

### ハンドオフ

ハンドオフ は、エージェント が委譲できるサブ エージェント です。ハンドオフ が発生すると、委譲先のエージェント は会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一のタスクに特化して優れた性能を発揮する、モジュール式のエージェント を実現できます。詳しくは [ハンドオフ](handoffs.md) ドキュメントをご覧ください。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions. "
        "If they ask about booking, hand off to the booking agent. "
        "If they ask about refunds, hand off to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 動的 instructions

多くの場合、エージェント を作成する際に instructions を指定します。ただし、関数を介して動的な instructions を提供することもできます。関数はエージェント とコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方を受け付けます。

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

場合によっては、エージェント のライフサイクルを観測したいことがあります。例えば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりする場合です。`hooks` プロパティでエージェント のライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドを override してください。

## ガードレール

ガードレール は、エージェント の実行と並行してユーザー 入力に対するチェック/検証を行い、またエージェント の出力が生成された後にも実行できます。例えば、ユーザー の入力やエージェント の出力の関連性をスクリーニングできます。詳しくは [ガードレール](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン/コピー

エージェント の `clone()` メソッドを使うと、エージェント を複製し、任意で好きなプロパティを変更できます。

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

ツールのリストを指定しても、LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM にツールを使用するかどうかの判断を任せます。
2. `required`: LLM にツールの使用を要求します（ただし、どのツールかは賢く判断できます）。
3. `none`: LLM にツールを _使用しない_ ことを要求します。
4. 具体的な文字列（例: `my_tool`）を設定すると、LLM にその特定のツールを使用することを要求します。

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

## ツール使用時の挙動

`Agent` の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します。

- `"run_llm_again"`: デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を最終応答として使用し、その後の LLM による処理は行いません。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼び出された時点で停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を継続するかを決定するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で構成できます。無限ループは、ツール結果が LLM に送られ、`tool_choice` により LLM がさらに別のツール呼び出しを生成し続けることで起こります。