---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中核となる基本コンポーネントです。エージェントは、instructions と tools で構成された大規模言語モデル（ LLM ）です。

## 基本構成

よく設定するエージェントのプロパティは以下のとおりです。

- `name`: エージェントを識別する必須の文字列です。
- `instructions`: developer message または システムプロンプト とも呼ばれます。
- `model`: どの LLM を使用するか、また任意の `model_settings` で temperature、top_p などのモデル調整パラメーターを設定できます。
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

エージェントはその `context` 型についてジェネリックです。コンテキストは依存性注入のためのツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、実行時の依存関係や状態をまとめて保持します。コンテキストには任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）の出力を生成します。特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用します。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使いますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型（dataclasses、lists、TypedDict など）をサポートします。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## 複数エージェントのシステム設計パターン

マルチエージェントシステムの設計方法は多数ありますが、幅広く適用できるパターンとして次の 2 つがよく使われます。

1. マネージャー（ツールとしてのエージェント）: 中央のマネージャー／オーケストレーターが、ツールとして公開された専門サブエージェントを呼び出し、会話の主導権を保持します。
2. ハンドオフ: 対等なエージェント同士が、会話を引き継ぐ専門エージェントに主導権を渡します。これは分散型です。

詳細は、[エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)をご覧ください。

### マネージャー（ツールとしてのエージェント）

`customer_facing_agent` がすべてのユーザーとのやり取りを担当し、ツールとして公開された専門サブエージェントを呼び出します。詳しくは [ツール](tools.md#agents-as-tools) のドキュメントをご覧ください。

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

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフが発生すると、委任先のエージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに長けたモジュール式・専門特化のエージェントを実現できます。詳しくは [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

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

多くの場合、エージェントを作成するときに instructions を指定しますが、関数を通じて動的な instructions を提供することもできます。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が利用できます。

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

場合によっては、エージェントのライフサイクルを観察したいことがあります。たとえば、イベントをログ出力したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティでエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールにより、エージェントの実行と並行して ユーザー入力 に対するチェック／検証を行い、また、エージェントが出力を生成した後にその出力に対してもチェックを実行できます。たとえば、ユーザー入力とエージェント出力を関連性でスクリーニングできます。詳しくは [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使うと、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを渡しても、常に LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定するとツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使用するかどうかを LLM に任せます。
2. `required`: LLM にツールの使用を必須にします（どのツールを使うかは知的に判断します）。
3. `none`: LLM にツールを _使用しない_ ことを要求します。
4. 特定の文字列（例: `my_tool`）を設定: LLM にその特定のツールの使用を要求します。

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

`Agent` の設定パラメーター `tool_use_behavior` は、ツール出力の取り扱い方法を制御します。

- `"run_llm_again"`: デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼び出されたら停止し、その出力を最終応答として使用します.

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送られ、`tool_choice` により LLM が再度ツール呼び出しを生成し続けることによって発生します。