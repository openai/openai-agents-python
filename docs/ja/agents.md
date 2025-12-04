---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中核となる構成要素です。エージェントは、 instructions とツールで構成された大規模言語モデル ( LLM ) です。

## 基本構成

エージェントで一般的に設定するプロパティは次のとおりです:

- `name`: エージェントを識別する必須の文字列です。
- `instructions`: developer message または system prompt とも呼ばれます。
- `model`: 使用する LLM と、 temperature、 top_p などのモデル調整パラメーターを設定するための任意の `model_settings`。
- `tools`: エージェントがタスクを達成するために使用できるツールです。

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

エージェントはその `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールです。あなたが作成して `Runner.run()` に渡すオブジェクトで、すべてのエージェント、ツール、ハンドオフなどに渡され、エージェントの実行における依存関係と状態の入れ物として機能します。任意の Python オブジェクトをコンテキストとして提供できます。

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

デフォルトでは、エージェントはプレーンテキスト (すなわち `str`) の出力を生成します。特定の型の出力をエージェントに生成させたい場合は、 `output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトの使用ですが、 Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型 ( dataclasses、 lists、 TypedDict など) をサポートします。

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

## マルチ エージェント システムの設計パターン

マルチ エージェント システムを設計する方法は多くありますが、一般的に広く適用できるパターンは次の 2 つです:

1. マネージャー (エージェントをツールとして): 中央のマネージャー/オーケストレーターが、ツールとして公開された特化サブ エージェントを呼び出し、会話の制御を保持します。
2. ハンドオフ: ピア エージェントが、会話を引き継ぐ特化エージェントに制御をハンドオフします。これは分散型です。

詳細は [実践的なエージェント構築ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### マネージャー (エージェントをツールとして)

`customer_facing_agent` はすべてのユーザー対応を処理し、ツールとして公開された特化サブ エージェントを呼び出します。詳しくは [ツール](tools.md#agents-as-tools) ドキュメントをご覧ください。

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

ハンドオフは、エージェントが委譲できるサブ エージェントです。ハンドオフが発生すると、委譲先のエージェントは会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに優れたモジュール型・特化型のエージェントを実現できます。詳しくは [ハンドオフ](handoffs.md) ドキュメントをご覧ください。

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

多くの場合、エージェントの作成時に instructions を指定できますが、関数を介して動的な instructions を提供することもできます。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数のどちらも使用できます。

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

## ライフサイクルイベント (フック)

場合によっては、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントをログに記録したり、特定のイベントが発生したときにデータを事前取得したりです。`hooks` プロパティで、エージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対するチェック/バリデーションを実行し、エージェントの出力が生成された後にその出力に対しても実行できます。たとえば、ユーザーの入力とエージェントの出力の妥当性をスクリーニングできます。詳しくは [ガードレール](guardrails.md) ドキュメントをご覧ください。

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

ツールのリストを提供しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです:

1. `auto`: ツールを使用するかどうかを LLM に任せます。
2. `required`: LLM にツールの使用を必須にします (どのツールを使うかは賢く判断できます)。
3. `none`: LLM にツールを使用しないことを必須にします。
4. 具体的な文字列 (例: `my_tool`) を設定し、 LLM にその特定のツールを使用させます。

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

`Agent` 構成の `tool_use_behavior` パラメーターは、ツール出力の処理方法を制御します:

- `"run_llm_again"`: デフォルト。ツールが実行され、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、その後の LLM 処理なしで最終応答として使用します。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループの原因は、ツール結果が LLM に送られ、`tool_choice` によって LLM がさらに別のツール呼び出しを生成し続けるためです。