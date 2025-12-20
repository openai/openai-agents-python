---
search:
  exclude: true
---
# エージェント

エージェント はアプリの中核となる構成要素です。エージェント は大規模言語モデル（ LLM ）で、 instructions とツールで構成されます。

## 基本設定

一般的に設定する エージェント の主なプロパティは次のとおりです。

- `name`: エージェント を識別する必須の文字列。
- `instructions`: developer メッセージ、または システムプロンプト とも呼ばれます。
- `model`: 使用する LLM と、temperature、top_p などのモデル調整パラメーターを設定するオプションの `model_settings`。
- `tools`: エージェント がタスクを達成するために利用できるツール。

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

エージェント はその `context` 型に対してジェネリックです。Context は依存性注入のためのツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべての エージェント、ツール、ハンドオフ などに渡され、エージェント 実行のための依存関係や状態をまとめて保持します。コンテキストには任意の Python オブジェクトを指定できます。

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

デフォルトでは、エージェント はプレーンテキスト（すなわち `str`）の出力を生成します。特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用します。一般的な選択肢としては [Pydantic](https://docs.pydantic.dev/) オブジェクトがありますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型（dataclasses、lists、TypedDict など）をサポートします。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答の代わりに [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するように指示されます。

## マルチ エージェント システムの設計パターン

マルチ エージェント システムを設計する方法は多数ありますが、一般的に広く適用できるパターンとして次の 2 つがよく見られます。

1. マネージャー（エージェント をツールとして）: 中央のマネージャー/オーケストレーターが、ツールとして公開された専門のサブ エージェント を呼び出し、会話の制御を維持します。
2. ハンドオフ: ピアの エージェント が、会話を引き継ぐ専門の エージェント に制御を引き渡します。これは分散型です。

詳細は [エージェント 構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### マネージャー（エージェント をツールとして）

`customer_facing_agent` はすべての ユーザー とのやり取りを担当し、ツールとして公開された専門のサブ エージェント を呼び出します。詳しくは [ツール](tools.md#agents-as-tools) のドキュメントをご覧ください。

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

ハンドオフ は、エージェント が委譲できるサブ エージェント です。ハンドオフ が発生すると、委譲先の エージェント は会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一のタスクに秀でたモジュール式の専門 エージェント を実現できます。詳しくは [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

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

## 動的な instructions

多くの場合、エージェント 作成時に instructions を指定できますが、関数を通じて動的な instructions を提供することもできます。関数は エージェント とコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数のどちらも利用できます。

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

エージェント のライフサイクルを観測したい場合があります。例えば、イベントをログに記録したり、特定のイベントが起こった際にデータを事前取得したいときです。`hooks` プロパティを使って エージェント のライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレール により、エージェント の実行と並行して ユーザー 入力に対するチェック/検証を行い、さらに エージェント の出力が生成された際にもチェック/検証を実行できます。たとえば、ユーザー の入力や エージェント の出力が関連性のある内容かをスクリーニングできます。詳しくは [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントの複製/コピー

エージェント の `clone()` メソッドを使用すると、エージェント を複製し、必要に応じて任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.2",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールの一覧を指定しても、LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM にツールを使用するかどうかを判断させます。
2. `required`: LLM にツールの使用を必須にします（ただし、どのツールを使うかは賢く選択できます）。
3. `none`: LLM にツールを使用しないことを必須にします。
4. 具体的な文字列（例: `my_tool`）を設定すると、LLM にその特定のツールの使用を必須にします。

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

`Agent` の設定にある `tool_use_behavior` パラメーターは、ツールの出力をどのように扱うかを制御します。

- `"run_llm_again"`: 既定。ツールを実行し、LLM が結果を処理して最終応答を生成します。
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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼ばれた場合に停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を続行するかを判断するカスタム関数。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツール結果が LLM に送られ、その `tool_choice` により LLM がさらに別のツール呼び出しを生成し続けることが原因です。