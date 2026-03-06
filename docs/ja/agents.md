---
search:
  exclude: true
---
# エージェント

エージェントはアプリ内の中核となる基本構成要素です。エージェントは、instructions、tools、および handoffs、ガードレール、structured outputs などの任意の実行時動作で設定された大規模言語モデル (LLM) です。

このページは、単一のエージェントを定義またはカスタマイズしたい場合に使用します。複数のエージェントをどのように連携させるべきかを検討している場合は、[エージェントオーケストレーション](multi_agent.md) をお読みください。

## 次のガイドの選択

このページをエージェント定義のハブとして使用してください。次に必要な意思決定に合う隣接ガイドへ進んでください。

| したいこと | 次に読むもの |
| --- | --- |
| モデルまたはプロバイダーのセットアップを選ぶ | [モデル](models/index.md) |
| エージェントに機能を追加する | [ツール](tools.md) |
| マネージャー形式のオーケストレーションと handoffs のどちらにするか決める | [エージェントオーケストレーション](multi_agent.md) |
| handoff の動作を設定する | [Handoffs](handoffs.md) |
| ターン実行、イベントのストリーミング、または会話状態の管理を行う | [エージェントの実行](running_agents.md) |
| 最終出力、実行項目、または再開可能な状態を確認する | [結果](results.md) |
| ローカル依存関係と実行時状態を共有する | [コンテキスト管理](context.md) |

## 基本設定

エージェントの最も一般的なプロパティは次のとおりです。

| プロパティ | 必須 | 説明 |
| --- | --- | --- |
| `name` | はい | 人間が読みやすいエージェント名。 |
| `instructions` | はい | システムプロンプトまたは動的 instructions コールバック。[動的 instructions](#dynamic-instructions) を参照してください。 |
| `prompt` | いいえ | OpenAI Responses API のプロンプト設定。静的なプロンプトオブジェクトまたは関数を受け付けます。[プロンプトテンプレート](#prompt-templates) を参照してください。 |
| `handoff_description` | いいえ | このエージェントが handoff 先として提示されるときに公開される短い説明。 |
| `handoffs` | いいえ | 会話を専門エージェントに委任します。[handoffs](handoffs.md) を参照してください。 |
| `model` | いいえ | 使用する LLM。[モデル](models/index.md) を参照してください。 |
| `model_settings` | いいえ | `temperature`、`top_p`、`tool_choice` などのモデル調整パラメーター。 |
| `tools` | いいえ | エージェントが呼び出せるツール。[ツール](tools.md) を参照してください。 |
| `mcp_servers` | いいえ | エージェント向けの MCP ベースのツール。[MCP ガイド](mcp.md) を参照してください。 |
| `input_guardrails` | いいえ | このエージェントチェーンの最初のユーザー入力で実行されるガードレール。[Guardrails](guardrails.md) を参照してください。 |
| `output_guardrails` | いいえ | このエージェントの最終出力で実行されるガードレール。[Guardrails](guardrails.md) を参照してください。 |
| `output_type` | いいえ | プレーンテキストの代わりに構造化出力の型を指定します。[出力型](#output-types) を参照してください。 |
| `tool_use_behavior` | いいえ | ツール結果をモデルに戻すか、実行を終了するかを制御します。[ツール使用動作](#tool-use-behavior) を参照してください。 |
| `reset_tool_choice` | いいえ | ツール使用ループを避けるため、ツール呼び出し後に `tool_choice` をリセットします (デフォルト: `True`)。[ツール使用の強制](#forcing-tool-use) を参照してください。 |

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

`prompt` を設定することで、OpenAI プラットフォームで作成したプロンプトテンプレートを参照できます。これは Responses API を使用する OpenAI モデルで動作します。

使用するには、次を実行してください。

1. https://platform.openai.com/playground/prompts に移動します
2. 新しいプロンプト変数 `poem_style` を作成します。
3. 次の内容でシステムプロンプトを作成します。

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` フラグを付けて例を実行します。

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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入ツールです。これは作成して `Runner.run()` に渡すオブジェクトで、すべてのエージェント、ツール、handoff などに渡され、エージェント実行の依存関係と状態をまとめる入れ物として機能します。任意の Python オブジェクトをコンテキストとして提供できます。

`RunContextWrapper` の完全な仕様、共有使用量トラッキング、ネストされた `tool_input`、およびシリアライズ時の注意点については、[context ガイド](context.md) をお読みください。

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

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) を出力します。エージェントに特定の型の出力をさせたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型であれば何でもサポートします。dataclasses、lists、TypedDict などです。

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

## マルチエージェントシステム設計パターン

マルチエージェントシステムの設計方法は多数ありますが、広く適用できる 2 つのパターンがよく見られます。

1. マネージャー (Agents as tools): 中央のマネージャー / オーケストレーターが専門サブエージェントをツールとして呼び出し、会話の制御を保持します。
2. Handoffs: ピアエージェントが制御を専門エージェントへ handoff し、そのエージェントが会話を引き継ぎます。これは分散型です。

詳細は [エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### マネージャー (Agents as tools)

`customer_facing_agent` はすべてのユーザー対話を処理し、ツールとして公開された専門サブエージェントを呼び出します。詳細は [tools](tools.md#agents-as-tools) のドキュメントを参照してください。

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

### Handoffs

Handoffs は、エージェントが委任できるサブエージェントです。handoff が発生すると、委任先エージェントは会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに特化して高い性能を発揮するモジュール化された専門エージェントを実現できます。詳細は [handoffs](handoffs.md) のドキュメントを参照してください。

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

多くの場合、エージェント作成時に instructions を指定できます。ただし、関数を介して動的 instructions を提供することもできます。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が受け付けられます。

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

場合によっては、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントのログ記録、データの事前取得、特定イベント発生時の使用量記録などです。

hook のスコープは 2 種類あります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他エージェントへの handoffs を含む `Runner.run(...)` 呼び出し全体を観測します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は、`agent.hooks` を介して特定のエージェントインスタンスにアタッチされます。

コールバックコンテキストもイベントに応じて変わります。

-   エージェント開始 / 終了 hook は [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。これは元のコンテキストをラップし、共有された実行使用量状態を保持します。
-   LLM、ツール、handoff の hook は [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

典型的な hook のタイミング:

-   `on_agent_start` / `on_agent_end`: 特定のエージェントが最終出力の生成を開始 / 終了したとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前 / 直後。
-   `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。
-   `on_handoff`: 制御があるエージェントから別のエージェントへ移るとき。

ワークフロー全体を単一の観測者で見たい場合は `RunHooks` を、1 つのエージェントにカスタム副作用が必要な場合は `AgentHooks` を使用してください。

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

コールバックの完全な仕様は、[Lifecycle API リファレンス](ref/lifecycle.md) を参照してください。

## ガードレール

ガードレールを使うと、エージェントの実行と並行してユーザー入力のチェック / 検証を実行し、さらにエージェント出力生成後にもチェック / 検証を実行できます。たとえば、ユーザー入力とエージェント出力の関連性を検査できます。詳細は [guardrails](guardrails.md) のドキュメントを参照してください。

## エージェントの複製 / コピー

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

ツール一覧を渡しても、LLM が必ずツールを使うとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM がツールを使用するかどうかを決定できます。
2. `required`: LLM にツール使用を必須にします (ただし、どのツールを使うかは賢く判断できます)。
3. `none`: LLM にツールを _使用しない_ ことを必須にします。
4. `my_tool` のような特定文字列: LLM にその特定ツールの使用を必須にします。

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

`Agent` 設定の `tool_use_behavior` パラメーターは、ツール出力をどのように処理するかを制御します。

- `"run_llm_again"`: デフォルトです。ツールを実行し、LLM が結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、追加の LLM 処理なしで最終応答として使用します。

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

- `StopAtTools(stop_at_tool_names=[...])`: 指定されたツールのいずれかが呼び出された場合に停止し、その出力を最終応答として使用します。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループが起きる理由は、ツール結果が LLM に送信され、`tool_choice` の影響で LLM がさらに別のツール呼び出しを生成し、これが際限なく続くためです。