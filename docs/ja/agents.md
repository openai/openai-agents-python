---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核の構成要素です。エージェントとは、instructions と tools を設定した大規模言語モデル (LLM) です。

## 基本設定

エージェントで設定することが多いプロパティは次のとおりです。

-   `name`: エージェントを識別するための必須の文字列です。
-   `instructions`: developer message または system prompt とも呼ばれます。
-   `model`: 使用する LLM、および temperature、top_p などのモデル調整パラメーターを設定するための任意の `model_settings` です。
-   `prompt`: OpenAI の Responses API を使用する際に、id (および変数) で prompt template を参照します。
-   `tools`: エージェントがタスクを達成するために使用できるツールです。
-   `mcp_servers`: エージェントに tools を提供する MCP サーバーです。[MCP ガイド](mcp.md) を参照してください。
-   `reset_tool_choice`: ツール使用ループを避けるため、ツール呼び出し後に `tool_choice` をリセットするかどうか (デフォルト: `True`) です。[ツール使用の強制](#forcing-tool-use) を参照してください。

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

## プロンプトテンプレート

`prompt` を設定することで、OpenAI プラットフォームで作成した prompt template を参照できます。これは、Responses API を使用する OpenAI モデルで動作します。

使用するには、次を行ってください。

1. https://platform.openai.com/playground/prompts に移動します。
2. 新しい prompt 変数 `poem_style` を作成します。
3. 次の内容で system prompt を作成します。

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` フラグで例を実行します。

```python
from agents import Agent

agent = Agent(
    name="Prompted assistant",
    prompt={
        "id": "pmpt_123",
        "version": "1",
        "variables": {"poem_style": "haiku"},
    },
)
```

実行時に prompt を動的に生成することもできます。

```python
from dataclasses import dataclass

from agents import Agent, GenerateDynamicPromptData, Runner

@dataclass
class PromptContext:
    prompt_id: str
    poem_style: str


async def build_prompt(data: GenerateDynamicPromptData):
    ctx: PromptContext = data.context.context
    return {
        "id": ctx.prompt_id,
        "version": "1",
        "variables": {"poem_style": ctx.poem_style},
    }


agent = Agent(name="Prompted assistant", prompt=build_prompt)
result = await Runner.run(
    agent,
    "Say hello",
    context=PromptContext(prompt_id="pmpt_123", poem_style="limerick"),
)
```

## コンテキスト

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入ツールであり、作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行のための依存関係と状態をまとめて保持する入れ物として機能します。コンテキストには任意の Python オブジェクトを指定できます。

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

## 出力型

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) を出力します。特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢としては [Pydantic](https://docs.pydantic.dev/) オブジェクトがありますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型 (dataclasses、リスト、TypedDict など) をサポートしています。

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

    `output_type` を渡すと、通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するようモデルに指示します。

## マルチエージェントシステムの設計パターン

マルチエージェントシステムの設計方法は多数ありますが、一般に広く適用できるパターンとして次の 2 つがよく見られます。

1. Manager (agents as tools): 中央のマネージャー/オーケストレーターが、専門化されたサブエージェントをツールとして呼び出し、会話の制御を保持します。
2. Handoffs: ピアエージェントが、会話を引き継ぐ専門化エージェントへ制御をハンドオフします。これは分散型です。

詳細は、[エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### Manager (agents as tools)

`customer_facing_agent` はユーザーとのすべてのやり取りを担い、ツールとして公開された専門化サブエージェントを呼び出します。詳しくは [tools](tools.md#agents-as-tools) ドキュメントを参照してください。

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

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフが発生すると、委任先のエージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに特化して優れた性能を発揮する、モジュール化された専門エージェントを実現できます。詳しくは [handoffs](handoffs.md) ドキュメントを参照してください。

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

## 動的 instructions

多くの場合、エージェント作成時に instructions を指定できます。ただし、関数を介して動的 instructions を指定することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方を使用できます。

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

場合によっては、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりする場合です。`hooks` プロパティでエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールにより、エージェントの実行と並行してユーザー入力に対するチェック/バリデーションを行い、生成後のエージェント出力に対してもチェックを行えます。たとえば、ユーザー入力とエージェント出力を関連性でスクリーニングできます。詳しくは [guardrails](guardrails.md) ドキュメントを参照してください。

## エージェントのクローン/コピー

エージェントの `clone()` メソッドを使用すると、Agent を複製し、必要に応じて任意のプロパティを変更できます。

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

ツールのリストを指定しても、LLM が必ずしもツールを使うとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使用するかどうかを LLM が判断します。
2. `required`: LLM にツール使用を必須にします (どのツールにするかは賢く判断できます)。
3. `none`: LLM にツールを使用しないことを必須にします。
4. 具体的な文字列 (例: `my_tool`) を設定: LLM にその特定のツールを使用することを必須にします。

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

`Agent` 設定における `tool_use_behavior` パラメーターは、ツール出力の取り扱い方法を制御します。

- `"run_llm_again"`: デフォルトです。ツールを実行し、LLM が結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、追加の LLM 処理なしに最終応答として使用します。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したツールのいずれかが呼び出された場合に停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM を継続するかを判断するカスタム関数です。

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

    無限ループを防ぐため、このフレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループが起きるのは、ツール結果が LLM に送られ、その後 `tool_choice` により別のツール呼び出しが生成され、これが際限なく続くためです。