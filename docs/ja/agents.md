---
search:
  exclude: true
---
# エージェント

エージェントは、アプリの中心的な構成要素です。エージェントとは、 instructions とツールで設定された大規模言語モデル（ LLM ）です。

## 基本設定

エージェントでよく設定するプロパティは次のとおりです:

-   `name`: エージェントを識別する必須の文字列。
-   `instructions`: developer message や system prompt とも呼ばれます。
-   `model`: どの LLM を使用するかを指定し、`model_settings` を使って temperature 、 top_p などのモデル調整パラメーターをオプションで設定できます。
-   `tools`: エージェントがタスク達成のために使用できるツール。

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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールで、`Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行のための依存関係や状態を入れておく入れ物として機能します。任意の Python オブジェクトをコンテキストとして渡せます。

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

デフォルトでは、エージェントはプレーンテキスト ( つまり `str` ) を出力します。特定の型で出力させたい場合は、`output_type` パラメーターを使用できます。よく使われるのは [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、 Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型であれば何でもサポートしています — dataclass 、 list 、 TypedDict などです。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく structured outputs を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが処理を委任できるサブエージェントです。ハンドオフのリストを提供すると、必要に応じてエージェントはそれらへ委任できます。これにより、単一タスクに特化したモジュール式のエージェントを編成できる強力なパターンが実現します。詳細は [handoffs](handoffs.md) のドキュメントをご覧ください。

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

通常、エージェント作成時に instructions を渡しますが、関数を介して動的に instructions を生成することもできます。その関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数でも `async` 関数でも構いません。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、特定のイベントが発生した際にログを出力したり、データを事前取得したりしたいケースです。そのような場合は `hooks` プロパティでライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] を継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対してチェックやバリデーションを実行できます。たとえば、ユーザーの入力を関連性でフィルタリングすることが可能です。詳細は [guardrails](guardrails.md) のドキュメントをご覧ください。

## エージェントの複製／コピー

`clone()` メソッドを使うと、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを渡しても、 LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は以下のとおりです:

1. `auto` : LLM がツールを使用するかどうかを判断します。  
2. `required` : LLM にツール使用を必須とします ( ただしどのツールを使うかは自動判断 )。  
3. `none` : LLM にツールを使用しないことを要求します。  
4. 文字列 ( 例: `my_tool` ) を指定すると、その特定のツールを必ず使用させます。

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

`Agent` の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します:  
- `"run_llm_again"` : デフォルト。ツールを実行した後、その結果を LLM が処理して最終応答を生成します。  
- `"stop_on_first_tool"` : 最初のツール呼び出しの出力をそのまま最終応答として使用し、以降の LLM 処理は行いません。

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

- `StopAtTools(stop_at_tool_names=[...])` : 指定したいずれかのツールが呼び出された時点で停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction` : ツール結果を処理し、停止するか LLM を続行するかを決定するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループが起こるのは、ツールの結果が LLM へ送られ、その `tool_choice` により再度ツール呼び出しが生成される、というサイクルが延々と続くためです。