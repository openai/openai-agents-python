---
search:
  exclude: true
---
# エージェント

エージェントはアプリのコアとなる構成要素です。エージェントは、大規模言語モデル ( LLM ) に instructions と tools を設定したものです。

## 基本設定

エージェントで最もよく設定するプロパティは次のとおりです:

-   `name`: エージェントを識別する必須の文字列。  
-   `instructions`: developer メッセージ、または system prompt とも呼ばれます。  
-   `model`: 使用する LLM と、temperature や top_p などのチューニングパラメーターを設定する `model_settings` (任意)。  
-   `tools`: エージェントがタスクを遂行するために使用できる tools。  

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

エージェントはその `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールで、`Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、tool、handoff などに渡され、エージェント実行時の依存関係や状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして渡せます。

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

デフォルトでは、エージェントはプレーンテキスト ( すなわち `str` ) を出力します。特定の型で出力させたい場合は `output_type` パラメーターを使用します。よく使われるのは [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型 ―  dataclass 、 list 、 TypedDict など ― であれば何でもサポートしています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するようになります。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは必要に応じてそれらに委任できます。これは、単一タスクに特化したモジュール型エージェントをオーケストレーションする強力なパターンです。詳細は [handoffs](handoffs.md) のドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を渡せますが、関数を使って動的に生成することも可能です。その関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方に対応しています。

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

エージェントのライフサイクルを観察したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりするケースです。`hooks` プロパティを使用してエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェント実行と並行してユーザー入力のチェック / バリデーションを行えます。たとえば、ユーザー入力の関連性を確認することができます。詳細は [guardrails](guardrails.md) のドキュメントをご覧ください。

## エージェントのクローン / 複製

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

tool のリストを指定しても、 LLM が必ずしも tool を使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、tool 使用を強制できます。有効な値は次のとおりです:

1. `auto` : LLM が tool を使うかどうかを判断します。  
2. `required` : LLM に tool の使用を必須とさせます ( どの tool を使うかは判断できます )。  
3. `none` : LLM に tool を使用しないことを要求します。  
4. 文字列を指定 ( 例: `my_tool` ) : 指定した tool の使用を要求します。  

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

## ツール使用時の動作

`Agent` の `tool_use_behavior` パラメーターは、tool の出力をどのように扱うかを制御します:
- `"run_llm_again"` : デフォルト。tool を実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"` : 最初の tool 呼び出しの出力をそのまま最終応答として使用し、以降の LLM 処理を行いません。

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

- `StopAtTools(stop_at_tool_names=[...])` : 指定した tool が呼び出された時点で停止し、その出力を最終応答として使用します。  
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
- `ToolsToFinalOutputFunction` : tool の結果を処理し、停止するか LLM を継続するかを判断するカスタム関数です。  

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

    無限ループを防ぐため、フレームワークは tool 呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、tool の結果が LLM に渡され、`tool_choice` の指定により再び tool 呼び出しが生成される、というサイクルが続くことが原因です。