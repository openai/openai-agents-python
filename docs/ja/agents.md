---
search:
  exclude: true
---
# エージェント

エージェント はアプリの中核となる構成要素です。エージェント は、 instructions とツールを設定した大規模言語モデル（ LLM ）です。

## 基本設定

エージェント で最も一般的に設定するプロパティは次のとおりです。

-   `name`: エージェント を識別する必須の文字列です。
-   `instructions`: developer message または system prompt とも呼ばれます。
-   `model`: 使用する LLM と、temperature、top_p などのモデル調整パラメーターを設定するオプションの `model_settings`。
-   `tools`: エージェント がタスクを達成するために使用できるツール。

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

エージェント はその `context` 型に対してジェネリックです。コンテキストは依存性注入のための道具で、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフ などに渡され、エージェント 実行のための依存関係と状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして提供できます。

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

デフォルトでは、エージェント はプレーンテキスト（`str`）を出力します。特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型（dataclasses、リスト、TypedDict など）をサポートしています。

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

## マルチエージェント システムの設計パターン

マルチエージェント システムの設計方法は多数ありますが、一般的に広く適用できるパターンとして次の 2 つがよく見られます。

1. マネージャー（エージェント をツールとして）: 中央のマネージャー/オーケストレーターが、ツールとして公開された専門のサブエージェント を呼び出し、会話の制御を保持します。
2. ハンドオフ: ピアのエージェント が制御を専門のエージェント に引き継ぎ、そのエージェント が会話を引き継ぎます。これは分散型です。

詳細は [エージェント 構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### マネージャー（エージェント をツールとして）

`customer_facing_agent` がすべてのユーザー 対応を処理し、ツールとして公開された専門のサブエージェント を呼び出します。詳細は [ツール](tools.md#agents-as-tools) ドキュメントを参照してください。

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

ハンドオフ は、エージェント が委任できるサブエージェント です。ハンドオフ が発生すると、委任先のエージェント は会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに特化したモジュール式のエージェント を実現できます。詳細は [ハンドオフ](handoffs.md) ドキュメントを参照してください。

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

多くの場合、エージェント を作成するときに instructions を指定できますが、関数を介して動的な instructions を提供することもできます。その関数はエージェント とコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が使用できます。

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

場合によっては、エージェント のライフサイクルを観測したいことがあります。例えば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりする場合です。`hooks` プロパティでエージェント のライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドします。

## ガードレール

ガードレール により、エージェント の実行と並行してユーザー 入力のチェック/検証を実行し、さらにエージェント の出力が生成された後にもチェック/検証を実行できます。例えば、ユーザー の入力やエージェント の出力の関連性をスクリーニングできます。詳細は [ガードレール](guardrails.md) ドキュメントを参照してください。

## エージェントのクローン/コピー

エージェント の `clone()` メソッドを使用すると、エージェント を複製し、必要に応じて任意のプロパティを変更できます。

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

ツールのリストを提供しても、LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`： LLM がツールを使用するかどうかを自分で判断します。
2. `required`： LLM にツールの使用を要求します（どのツールを使うかは賢く判断できます）。
3. `none`： LLM にツールを使用「しない」ことを要求します。
4. 特定の文字列（例: `my_tool`）を設定： LLM にその特定のツールを使用させます。

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

`Agent` の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します。

- `"run_llm_again"`: デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力をそのまま最終応答として使用し、追加の LLM 処理は行いません。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送られ、`tool_choice` により LLM が再度ツール呼び出しを生成し続けてしまうために発生します。