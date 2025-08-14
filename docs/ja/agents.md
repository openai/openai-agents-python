---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中心的な構成要素です。エージェントとは、instructions と tools で設定された大規模言語モデル (LLM) です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

-   `name`：エージェントを識別する必須の文字列。
-   `instructions`：開発者メッセージ、または system prompt とも呼ばれます。
-   `model`：使用する LLM。さらに `model_settings` で temperature や top_p などのモデル調整パラメーターを設定できます。
-   `tools`：エージェントがタスクを達成するために使用できるツール群。

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

エージェントは `context` 型についてジェネリックです。Context は依存性注入のための道具で、`Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、tool、handoff などに渡され、実行中の依存関係や状態をまとめて保持します。任意の Python オブジェクトを context として渡せます。

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

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) を出力します。特定の型で出力させたい場合は `output_type` パラメーターを使います。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な型 ― dataclass、list、TypedDict など ― であれば利用可能です。

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

    `output_type` を渡すと、モデルは通常のプレーンテキストの代わりに [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは必要に応じてそこへ委譲できます。これにより、単一タスクに特化したモジュール化されたエージェントを編成できる強力なパターンが実現します。詳細は [handoffs](handoffs.md) ドキュメントを参照してください。

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

多くの場合、エージェント作成時に instructions を指定しますが、関数経由で動的に渡すこともできます。この関数は agent と context を受け取り、プロンプトを返さなければなりません。同期関数と `async` 関数の両方を使用できます。

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

エージェントのライフサイクルを観測したい場合があります。たとえば、イベントをログに残したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティでライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力のチェックやバリデーションを実行できます。たとえば、ユーザー入力の関連性をフィルタリングできます。詳細は [guardrails](guardrails.md) ドキュメントを参照してください。

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

ツールのリストを渡しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定してツール使用を強制できます。有効な値は次のとおりです。

1. `auto`：LLM がツールを使うかどうかを判断します。
2. `required`：LLM にツール使用を必須とします (どのツールを使うかは LLM が判断)。
3. `none`：LLM にツールを使用しないことを要求します。
4. 特定の文字列 (例: `my_tool`) を設定すると、そのツールを必ず使用させます。

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

## ツール使用時の挙動

`Agent` の `tool_use_behavior` パラメーターは、ツールの出力をどのように処理するかを制御します。
- `"run_llm_again"`：デフォルト。ツールを実行し、その結果を LLM が処理して最終応答を生成します。
- `"stop_on_first_tool"`：最初のツール呼び出しの出力を最終応答として使用し、追加の LLM 処理を行いません。

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

- `StopAtTools(stop_at_tool_names=[...])`：指定したいずれかのツールが呼び出された時点で停止し、その出力を最終応答として使用します。  
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
- `ToolsToFinalOutputFunction`：ツール結果を処理し、停止するか LLM を続行するかを決定するカスタム関数です。

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

    無限ループを防止するため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。ツール結果が LLM に送られ、`tool_choice` により再度ツール呼び出しが生成される、という無限ループを防ぐためです。