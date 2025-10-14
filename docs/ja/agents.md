---
search:
  exclude: true
---
# エージェント

エージェントは、アプリの中核となる構成要素です。エージェントは、instructions とツールで構成された大規模言語モデル（LLM）です。

## 基本構成

エージェントで最も一般的に設定するプロパティは次のとおりです。

- `name`: エージェントを識別する必須の文字列です。
- `instructions`: developer メッセージ、または system prompt とも呼ばれます。
- `model`: 使用する LLM と、temperature、top_p などのモデル調整パラメーターを設定する任意の `model_settings`。
- `tools`: エージェントがタスク達成のために使用できるツールです。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="gpt-5-nano",
    tools=[get_weather],
)
```

## コンテキスト

エージェントは `context` 型に対して汎用的です。コンテキストは依存性注入のためのツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行のための依存関係と状態のまとめとして機能します。コンテキストには任意の Python オブジェクトを指定できます。

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

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）を出力します。特定のタイプの出力を生成したい場合は、`output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトを用いることですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能なあらゆる型（dataclasses、lists、TypedDict など）をサポートします。

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

## マルチエージェントのシステム設計パターン

マルチエージェントシステムの設計方法は多数ありますが、広く適用できる 2 つのパターンをよく見かけます。

1. マネージャー（エージェントをツールとして）: 中央のマネージャー/オーケストレーターが、ツールとして公開された専門のサブエージェントを呼び出し、会話の制御を保持します。
2. ハンドオフ: ピアのエージェントが制御を専門のエージェントに引き渡し、そのエージェントが会話を引き継ぎます。これは分散型です。

詳細は、[実践的なエージェント構築ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)をご覧ください。

### マネージャー（エージェントをツールとして）

`customer_facing_agent` はすべてのユーザーやり取りを処理し、ツールとして公開された専門のサブエージェントを呼び出します。詳細は [ツール](tools.md#agents-as-tools) ドキュメントを参照してください。

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

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフが発生すると、委任先のエージェントは会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一のタスクに特化して優れた能力を発揮する、モジュール式の専門エージェントが可能になります。詳細は [ハンドオフ](handoffs.md) ドキュメントを参照してください。

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

多くの場合、エージェントの作成時に instructions を指定できますが、関数を介して動的に instructions を提供することもできます。その関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が使用できます。

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

エージェントのライフサイクルを観察したい場合があります。たとえば、イベントをログに記録したり、特定のイベントが発生したときにデータを事前取得したりできます。`hooks` プロパティを使ってエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールにより、エージェントの実行と並行してユーザー入力に対するチェック/バリデーションを実行し、エージェントの出力生成後にはその出力に対するチェック/バリデーションを実行できます。たとえば、ユーザー入力とエージェント出力の関連性をスクリーニングできます。詳細は [ガードレール](guardrails.md) ドキュメントを参照してください。

## エージェントのクローン/コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-4.1",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールのリストを指定しても、LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使用するかどうかを LLM に委ねます。
2. `required`: LLM にツールの使用を要求します（どのツールを使うかは賢く判断できます）。
3. `none`: LLM にツールを使用しないことを要求します。
4. 具体的な文字列（例: `my_tool`）を設定し、その特定のツールを使用することを LLM に要求します。

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

## ツール使用の挙動

`Agent` 構成の `tool_use_behavior` パラメーターは、ツールの出力の扱い方を制御します。

- `"run_llm_again"`: デフォルト。ツールを実行し、LLM がその結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、その後の LLM 処理なしに最終応答として使用します。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼び出された場合に停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を継続するかを判断するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動で "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で構成可能です。無限ループは、ツール結果が LLM に送られ、`tool_choice` のために LLM が再びツール呼び出しを生成し続けることが原因です。