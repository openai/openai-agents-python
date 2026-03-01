---
search:
  exclude: true
---
# エージェント

エージェントはアプリ内の中核となる構成要素です。エージェントは instructions と tools で構成された大規模言語モデル (LLM) です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

-   `name`: エージェントを識別する必須の文字列です。
-   `instructions`: developer message またはシステムプロンプトとも呼ばれます。
-   `model`: 使用する LLM と、temperature、top_p などのモデル調整パラメーターを設定するための任意の `model_settings` です。
-   `prompt`: OpenAI の Responses API を使用する際に、id (および変数) でプロンプトテンプレートを参照します。
-   `tools`: エージェントがタスク達成のために使用できるツールです。
-   `mcp_servers`: エージェントにツールを提供する MCP サーバーです。[MCP ガイド](mcp.md)をご覧ください。
-   `reset_tool_choice`: ツール使用ループを避けるために、ツール呼び出し後に `tool_choice` をリセットするかどうか (既定値: `True`) です。[ツール使用の強制](#forcing-tool-use)をご覧ください。

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

`prompt` を設定することで、 OpenAI プラットフォームで作成したプロンプトテンプレートを参照できます。これは Responses API を使用する OpenAI モデルで動作します。

使用するには、次を行ってください。

1. https://platform.openai.com/playground/prompts にアクセスします
2. 新しいプロンプト変数 `poem_style` を作成します。
3. 次の内容でシステムプロンプトを作成します。

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` フラグ付きでコード例を実行します。

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

実行時にプロンプトを動的に生成することもできます。

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

エージェントは `context` 型に対して汎用的です。context は依存性注入ツールです。これは `Runner.run()` に渡すために作成するオブジェクトで、すべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行のための依存関係と状態をまとめて保持する入れ物として機能します。context には任意の Python オブジェクトを提供できます。

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

既定では、エージェントはプレーンテキスト (つまり `str`) を出力します。エージェントに特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトが選ばれますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な型であればサポートされます。たとえば dataclasses、lists、TypedDict などです。

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

    `output_type` を渡すと、通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するようモデルに指示されます。

## マルチエージェントシステムの設計パターン

マルチエージェントシステムの設計方法は多数ありますが、広く適用可能なパターンとして、一般的に次の 2 つが見られます。

1. Manager (agents as tools): 中央の manager/orchestrator が、ツールとして特化したサブエージェントを呼び出し、会話の制御を保持します。
2. ハンドオフ: 同等のエージェント同士が、会話を引き継ぐ特化エージェントへ制御をハンドオフします。これは分散型です。

詳細は [エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)をご覧ください。

### Manager (agents as tools)

`customer_facing_agent` はすべてのユーザーとのやり取りを処理し、ツールとして公開された特化サブエージェントを呼び出します。詳細は [tools](tools.md#agents-as-tools) ドキュメントをご覧ください。

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

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフが発生すると、委譲先エージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに特化して高い性能を発揮する、モジュール化されたエージェントが実現できます。詳細は [handoffs](handoffs.md) ドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を提供できます。ただし、関数を介して動的 instructions を提供することもできます。関数はエージェントと context を受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が使用できます。

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

場合によっては、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントログの記録、データの事前取得、特定イベント発生時の使用量記録などです。

hook のスコープは 2 種類あります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他エージェントへのハンドオフを含む `Runner.run(...)` 呼び出し全体を観測します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は `agent.hooks` を通じて特定のエージェントインスタンスにアタッチされます。

コールバックの context もイベントによって変わります。

-   エージェント開始/終了 hook は、元の context をラップし共有実行使用量状態を保持する [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。
-   LLM、ツール、ハンドオフ hook は [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

代表的な hook のタイミング:

-   `on_agent_start` / `on_agent_end`: 特定エージェントが最終出力の生成を開始/終了したとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前/直後。
-   `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。
-   `on_handoff`: 制御があるエージェントから別のエージェントへ移るとき。

ワークフロー全体を単一の観測者で見たい場合は `RunHooks` を、1 つのエージェントにカスタム副作用が必要な場合は `AgentHooks` を使用します。

```python
from agents import Agent, RunHooks, Runner


class LoggingHooks(RunHooks):
    async def on_agent_start(self, context, agent):
        print(f"Starting {agent.name}")

    async def on_llm_end(self, context, agent, response):
        print(f"{agent.name} produced {len(response.output)} output items")

    async def on_agent_end(self, context, agent, output):
        print(f"{agent.name} finished with usage: {context.usage}")


agent = Agent(name="Assistant", instructions="Be concise.")
result = await Runner.run(agent, "Explain quines", hooks=LoggingHooks())
print(result.final_output)
```

コールバックの完全な対象範囲は [Lifecycle API リファレンス](ref/lifecycle.md)をご覧ください。

## ガードレール

ガードレールを使用すると、エージェント実行と並行してユーザー入力のチェック/検証を実行し、さらにエージェント出力生成後にその出力をチェックできます。たとえば、ユーザー入力やエージェント出力の関連性をスクリーニングできます。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントの複製/コピー

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

tools のリストを指定しても、LLM が必ずツールを使うとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使うかどうかを LLM が判断します。
2. `required`: LLM にツール使用を必須化します (ただしどのツールを使うかは LLM が適切に判断できます)。
3. `none`: LLM がツールを使わないよう必須化します。
4. 例として `my_tool` のような特定文字列を設定: LLM にその特定ツールの使用を必須化します。

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

`Agent` 設定内の `tool_use_behavior` パラメーターは、ツール出力をどのように処理するかを制御します。

- `"run_llm_again"`: 既定値です。ツールを実行し、最終応答を生成するために LLM が結果を処理します。
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

- `StopAtTools(stop_at_tool_names=[...])`: 指定したいずれかのツールが呼び出された場合に停止し、その出力を最終応答として使用します。

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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM で続行するかを判断するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツール結果が LLM に送信され、`tool_choice` のために LLM がさらに別のツール呼び出しを生成し、これが際限なく続くことで発生します。