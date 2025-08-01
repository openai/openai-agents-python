---
search:
  exclude: true
---
# エージェント

エージェントはアプリのコアとなる構成要素です。エージェントは instructions とツールで設定された大規模言語モデル（LLM）です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

- `name`: エージェントを識別する必須の文字列。  
- `instructions`: developer メッセージまたは system prompt とも呼ばれます。  
- `model`: 使用する LLM、および temperature や top_p などのモデルチューニングパラメーターを設定する任意の `model_settings`。  
- `tools`: エージェントがタスクを達成するために使用できるツール。  

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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性インジェクション用のツールで、`Runner.run()` に渡すオブジェクトです。これがすべてのエージェント、ツール、ハンドオフなどに渡され、実行時の依存関係や状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして渡すことができます。

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

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）を出力します。特定の型で出力させたい場合は、`output_type` パラメーターを使用します。よく使われるのは [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型—dataclasses、list、TypedDict など—であれば何でもサポートされています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡しておくと、エージェントは適切な場合にそれらへ委任できます。これは、単一タスクに特化したモジュール式エージェントをオーケストレーションする強力なパターンです。詳細は [ハンドオフ](handoffs.md) のドキュメントを参照してください。

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

多くの場合、エージェント作成時に instructions を指定しますが、関数を介して動的に instructions を提供することもできます。その関数はエージェントとコンテキストを受け取り、プロンプトを返さなければなりません。同期関数・`async` 関数のどちらも使用できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティを利用してエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] を継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対するチェック／バリデーションを行えます。たとえば、ユーザー入力の関連性をスクリーニングすることが可能です。詳細は [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントの複製／コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

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

ツールのリストを指定しても、LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM がツールを使うかどうかを判断します。  
2. `required`: LLM にツール使用を必須とします（使用するツールは自動選択されます）。  
3. `none`: LLM にツールを使用しないことを要求します。  
4. 具体的な文字列（例: `my_tool`）を設定すると、その特定のツールの使用を要求します。  

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

## ツール使用動作

`Agent` の `tool_use_behavior` パラメーターはツール出力の扱いを制御します。  
- `"run_llm_again"`: デフォルト。ツールを実行した後、LLM が結果を処理して最終応答を生成します。  
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を最終応答として使用し、LLM による追加処理を行いません。  

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したツールのいずれかが呼び出された時点で停止し、その出力を最終応答として使用します。  
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
- `ToolsToFinalOutputFunction`: ツールの結果を処理し、停止するか LLM を継続するかを判断するカスタム関数です。  

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に自動で `tool_choice` を "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。ツール結果が LLM に送られ、`tool_choice` により再度ツール呼び出しが生成される…というループを防ぐためです。