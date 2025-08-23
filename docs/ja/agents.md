---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中核となる基本構成要素です。エージェントは、 instructions と tools で構成された大規模言語モデル  LLM  です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

-   `name`: エージェントを識別するための必須の文字列です。
-   `instructions`: developer message（開発者メッセージ）または system prompt とも呼ばれます。
-   `model`: 使用する  LLM  と、temperature、top_p などのモデルチューニング用の任意の `model_settings`。
-   `tools`: エージェントがタスクを達成するために使用できるツールです。

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

エージェントは `context` 型に対して汎用的です。コンテキストは依存性注入のためのツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェントの実行における依存関係や状態をまとめて保持します。コンテキストには任意の  Python  オブジェクトを指定できます。

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

既定では、エージェントはプレーンテキスト（つまり `str`）を出力します。特定の型の出力をエージェントに生成させたい場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできるあらゆる型（dataclasses、lists、TypedDict など）をサポートします。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく、 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用します。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを指定すると、必要に応じてエージェントがそれらに委譲できます。これは、単一のタスクに特化して優れた能力を発揮するモジュール型のエージェントをオーケストレーションできる強力なパターンです。詳細は [ハンドオフ](handoffs.md) のドキュメントを参照してください。

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

多くの場合、エージェント作成時に instructions を指定できますが、関数を介して動的に instructions を提供することも可能です。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数のどちらも使用できます。

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

ときには、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティでエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールにより、エージェントの実行と並行してユーザー入力に対するチェック／バリデーションを行い、さらにエージェントの出力が生成された後にもチェックを実施できます。たとえば、ユーザーの入力やエージェントの出力の関連性をスクリーニングできます。詳細は [ガードレール](guardrails.md) のドキュメントを参照してください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを指定しても、必ずしも  LLM  がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定してツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使用するかどうかを  LLM  に委ねます。
2. `required`:  LLM  にツールの使用を必須にします（どのツールを使うかは賢く判断します）。
3. `none`:  LLM  にツールを使用しないことを要求します。
4. 具体的な文字列（例: `my_tool`）を設定し、その特定のツールを  LLM  に使用させます。

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

`Agent` 構成の `tool_use_behavior` パラメーターは、ツールの出力の扱い方を制御します。
- `"run_llm_again"`: 既定。ツールを実行し、その結果を  LLM  が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、追加の  LLM  処理なしでそのまま最終応答として使用します。

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
- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか  LLM  を続行するかを判断するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツール結果が  LLM  に送られ、`tool_choice` により  LLM  が再びツール呼び出しを生成し続けてしまうことに起因します。