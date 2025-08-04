---
search:
  exclude: true
---
# エージェント

エージェントはアプリのコアとなる構成要素です。エージェントとは、 instructions と tools で設定された大規模言語モデル ( LLM ) です。

## 基本設定

最も一般的に設定するプロパティは次のとおりです。

- `name`: エージェントを識別する必須の文字列です。  
- `instructions`: developer message または system prompt とも呼ばれます。  
- `model`: 使用する LLM と、 temperature や top_p などのモデル調整パラメーターを指定できる `model_settings` ( 任意 )。  
- `tools`: エージェントがタスクを達成するために使用できる tools です。

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

エージェントは `context` の型がジェネリックです。 context は依存性注入のためのツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、 tool、 handoff などに渡され、依存関係や実行時の状態をまとめて保持します。 context には任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェントはプレーンテキスト ( つまり `str` ) を出力します。特定の型で出力させたい場合は `output_type` パラメーターを使用します。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使いますが、 dataclass や list、 TypedDict など、 Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型なら何でも対応しています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキストではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を利用して応答します。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。 handoffs のリストを渡すと、エージェントは必要に応じてそれらに委任できます。これは、単一タスクに特化したモジュール化されたエージェントを編成する強力なパターンです。詳細は [ハンドオフ](handoffs.md) ドキュメントをご覧ください。

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

## 動的インストラクション

多くの場合、エージェント作成時に instructions を渡しますが、関数を介して動的に instructions を生成することもできます。この関数は agent と context を受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方に対応しています。

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

## ライフサイクルイベント ( hooks )

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。 `hooks` プロパティでエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使うと、エージェント実行と並行してユーザー入力のチェックやバリデーションを実行できます。たとえば、ユーザー入力を関連性でフィルタリングするなどが可能です。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン / コピー

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

## ツール利用の強制

tools のリストを渡しても、 LLM が必ずしも tool を利用するとは限りません。 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定してツール利用を強制できます。使用可能な値は次のとおりです。

1. `auto` : LLM が tool を使うかどうかを決定します。  
2. `required` : LLM に tool の使用を必須にします ( どの tool を使うかは自動で判断 )。  
3. `none` : LLM に tool を _使わない_ ことを要求します。  
4. 具体的な文字列 ( 例: `my_tool` ) を指定すると、その特定の tool の使用を要求します。

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

## ツール利用の挙動

`Agent` の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します。  
- `"run_llm_again"` : 既定値。 tool を実行し、その結果を LLM が処理して最終応答を生成します。  
- `"stop_on_first_tool"` : 最初の tool 呼び出しの出力を最終応答として使用し、追加の LLM 処理は行いません。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかの tool が呼び出された時点で停止し、その出力を最終応答として使用します。  
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
- `ToolsToFinalOutputFunction`: tool の結果を処理し、 LLM を続行するか停止するかを判断するカスタム関数です。

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

    無限ループを防ぐため、フレームワークは tool 呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。ツール結果が LLM に送られ、その後 `tool_choice` により再度ツール呼び出しが生成されることで無限ループが発生するためです。