---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中核を成す構成要素です。エージェントとは、 instructions と tools で構成された大規模言語モデル ( LLM ) です。

## 基本設定

もっとも一般的に設定するエージェントのプロパティは次のとおりです。

- `name` : エージェントを識別する必須の文字列です。  
- `instructions` : developer message または system prompt とも呼ばれます。  
- `model` : 使用する LLM を指定します。また、 `model_settings` を用いて temperature や top_p などのチューニングパラメーターを任意で設定できます。  
- `tools` : エージェントがタスクを遂行するために使用できる tools です。  

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

エージェントは `context` 型についてジェネリックになっています。コンテキストは依存性注入のための仕組みで、あなたが作成して `Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、 tool 、 handoff などへ引き渡され、実行時の依存関係や状態を保持する入れ物として機能します。任意の Python オブジェクトをコンテキストとして渡せます。

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

デフォルトでは、エージェントはプレーンテキスト ( つまり `str` ) を出力します。特定の型で出力させたい場合は、 `output_type` パラメーターを使用します。よく使われるのは Pydantic オブジェクトですが、Pydantic の TypeAdapter にラップできる型であれば何でもサポートしています。たとえば dataclass 、 list 、 TypedDict などです。

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

    `output_type` を渡すと、モデルは通常のプレーンテキストの代わりに structured outputs を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは状況に応じてそれらへ委譲できます。これは、単一のタスクに特化したモジュラーなエージェントを編成できる強力なパターンです。詳細は handoffs のドキュメントをご覧ください。

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

ほとんどの場合、エージェント作成時に instructions を渡しますが、関数を通じて動的に instructions を生成することもできます。その関数は agent と context を受け取り、プロンプトを返す必要があります。同期関数と async 関数の両方を指定できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントを記録したり、特定のイベントが起きた際にデータを事前取得したりするケースです。 `hooks` プロパティを使ってエージェントのライフサイクルにフックできます。 `AgentHooks` クラスを継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使うと、エージェントの実行と並行してユーザー入力のチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をスクリーニングすることが可能です。詳細は guardrails のドキュメントを参照してください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使うと、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

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

tools のリストを渡しても、 LLM が必ずしもツールを使用するとは限りません。 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです:

1. `auto` : LLM がツールを使うかどうかを判断します。  
2. `required` : LLM にツール使用を必須とします ( どのツールを使うかは賢く選択されます )。  
3. `none` : LLM にツールを使用させません。  
4. 特定の文字列 ( 例 `my_tool` ) を設定すると、そのツールの使用を強制します。  

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

`Agent` の `tool_use_behavior` パラメーターは、ツールの出力をどのように扱うかを制御します:

- `"run_llm_again"` : デフォルト。ツールを実行し、その結果を LLM が処理して最終レスポンスを生成します。  
- `"stop_on_first_tool"` : 最初のツール呼び出しの出力をそのまま最終レスポンスとして使用し、以降 LLM は処理を行いません。  

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

- `StopAtTools(stop_at_tool_names=[...])` : 指定したツールが呼び出された時点で停止し、その出力を最終レスポンスとして使用します。  

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

- `ToolsToFinalOutputFunction` : ツール結果を処理し、停止するか LLM を続行するかを判断するカスタム関数です。  

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送信され、その `tool_choice` により再びツール呼び出しが生成されることを繰り返すために発生します。