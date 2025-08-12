---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中核となるビルディングブロックです。エージェントとは、指示とツールで構成された大規模言語モデル ( LLM ) です。

## 基本設定

エージェントで最もよく設定するプロパティは次のとおりです。

- `name`: エージェントを識別する必須の文字列。
- `instructions`: developer メッセージまたは system prompt とも呼ばれます。
- `model`: 使用する LLM、および temperature や top_p などのチューニングパラメーターを設定する `model_settings` (オプション)。
- `tools`: エージェントがタスク達成のために使用できるツール。

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

エージェントはその `context` 型に対して汎用的です。コンテキストは依存性注入ツールであり、あなたが作成して `Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行の依存関係や状態をまとめて保持します。コンテキストには任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェントはプレーンテキスト (すなわち `str`) を出力します。特定の型で出力させたい場合は、`output_type` パラメーターを使用します。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使いますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型―dataclass、list、TypedDict など―であれば利用できます。

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

    `output_type` を指定すると、モデルは通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを提供すると、エージェントは関連する場合にそれらへ委譲できます。これは、単一タスクに特化したモジュール型エージェントをオーケストレーションする強力なパターンです。詳しくは [handoffs](handoffs.md) ドキュメントをご覧ください。

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

通常はエージェント作成時に instructions を渡しますが、関数を通じて動的に渡すこともできます。この関数はエージェントと context を受け取り、プロンプトを返さなければなりません。同期関数と `async` 関数の両方を使用できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータをプリフェッチしたりするケースです。`hooks` プロパティを使ってエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] をサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使うと、エージェント実行と並行してユーザー入力に対するチェックやバリデーションを実行できます。たとえば、ユーザー入力の関連性をスクリーニングすることが可能です。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使うと、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを渡しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定するとツール使用を強制できます。有効な値は次のとおりです。

1. `auto` : LLM がツールを使用するかどうかを自動で判断します。
2. `required` : LLM にツール使用を必須とします (使用するツールはインテリジェントに決定)。
3. `none` : LLM にツールを使用しないことを要求します。
4. 具体的な文字列 (例: `my_tool`) を設定すると、そのツールを必ず使用させます。

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

`Agent` の `tool_use_behavior` パラメーターはツール出力の扱い方を制御します。

- `"run_llm_again"`: デフォルト設定。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初に呼び出されたツールの出力をそのまま最終応答として使用し、追加の LLM 処理を行いません。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したツールのいずれかが呼び出されると停止し、その出力を最終応答とします。  
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
- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を続行するかを決定するカスタム関数。  

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。ツール結果が LLM に送られ、その後 `tool_choice` により再度ツール呼び出しが生成され…という無限ループを防止するためです。