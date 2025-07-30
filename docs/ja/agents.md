---
search:
  exclude: true
---
# エージェント

エージェント は、アプリの中心的なビルディングブロックです。エージェント は、指示 (`instructions`) とツール (`tools`) で構成された LLM です。

## 基本設定

エージェント で最もよく設定するプロパティは次のとおりです。

- `name`:  エージェント を識別する必須の文字列です。  
- `instructions`:  開発者メッセージ、または システムプロンプト とも呼ばれます。  
- `model`:  使用する LLM を指定します。`model_settings` を使って temperature、top_p などのモデル チューニング パラメーターを設定できます。  
- `tools`:  エージェント がタスクを達成するために使用できるツールです。  

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

エージェント はその `context` 型についてジェネリックです。コンテキストは dependency-injection 用のオブジェクトで、あなたが作成して `Runner.run()` に渡し、実行中のエージェント・ツール・ハンドオフ などすべてに共有されます。実行に必要な依存関係や状態をまとめて保持する入れ物として機能します。コンテキストには任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェント はプレーンテキスト (つまり `str`) を出力します。特定の型で出力させたい場合は `output_type` パラメーターを使用します。一般的には [ Pydantic ](https://docs.pydantic.dev/) オブジェクトを使いますが、Pydantic の [ TypeAdapter ](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型 (dataclass、list、TypedDict など) なら何でもサポートされています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキストではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフ は、エージェント が委譲できるサブエージェントです。ハンドオフ のリストを渡すことで、関連性がある場合にエージェント がそれらへ委譲できます。これは、単一タスクに特化したモジュール化 エージェント をオーケストレーションする強力なパターンです。詳細は [ハンドオフ](handoffs.md) ドキュメントを参照してください。

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

多くの場合、エージェント 作成時に instructions を指定しますが、関数を使って動的に instructions を生成することもできます。この関数はエージェント と コンテキスト を受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方を使用できます。

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

## ライフサイクルイベント (hooks)

エージェント のライフサイクルを観察したい場合があります。たとえば、イベントをログに残したり、特定のイベント発生時にデータを事前取得したりするケースです。`hooks` プロパティを使って エージェント のライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] を継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレール を使うと、エージェント 実行と並行してユーザー入力に対するチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をフィルタリングできます。詳細は [guardrails](guardrails.md) ドキュメントを参照してください。

## エージェントの複製とコピー

`clone()` メソッドを使用すると、エージェント を複製し、必要に応じて任意のプロパティを変更できます。

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

ツールのリストを渡しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。指定できる値は次のとおりです。

1. `auto` :  LLM がツールを使うかどうかを判断します。  
2. `required` :  LLM にツール使用を必須とします (どのツールを使うかは自動判断)。  
3. `none` :  LLM にツールを使用しないよう必須とします。  
4. 文字列を指定 (例: `my_tool`) :  指定したツールを必ず使用させます。  

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

`Agent` の `tool_use_behavior` パラメーターは、ツールの出力をどのように扱うかを制御します。

- `"run_llm_again"`: デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。  
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を最終応答として使用し、追加の LLM 処理は行いません。  

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したツールが呼び出された時点で停止し、その出力を最終応答として使用します。  
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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動で `"auto"` にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送られ、`tool_choice` により LLM が再度ツールを呼び出し…という処理が繰り返されることで発生します。