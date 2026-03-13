---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核的な基本コンポーネントです。エージェントは、大規模言語モデル ( LLM ) に instructions、ツール、さらにハンドオフ、ガードレール、structured outputs などの任意の実行時動作を設定したものです。

このページは、単一のエージェントを定義またはカスタマイズしたい場合に使用します。複数のエージェントをどのように連携させるかを検討している場合は、[エージェントオーケストレーション](multi_agent.md) を参照してください。

## 次のガイドの選択

このページをエージェント定義のハブとして使用してください。次に必要な判断に対応する隣接ガイドへ移動できます。

| したいこと | 次に読むもの |
| --- | --- |
| モデルまたはプロバイダー設定を選ぶ | [Models](models/index.md) |
| エージェントに機能を追加する | [Tools](tools.md) |
| マネージャースタイルのオーケストレーションとハンドオフのどちらにするか決める | [エージェントオーケストレーション](multi_agent.md) |
| ハンドオフ動作を設定する | [Handoffs](handoffs.md) |
| ターン実行、イベントのストリーミング、会話状態の管理を行う | [エージェントの実行](running_agents.md) |
| 最終出力、実行項目、再開可能な状態を確認する | [結果](results.md) |
| ローカル依存関係と実行時状態を共有する | [コンテキスト管理](context.md) |

## 基本設定

エージェントの最も一般的なプロパティは次のとおりです。

| プロパティ | 必須 | 説明 |
| --- | --- | --- |
| `name` | はい | 人が読めるエージェント名です。 |
| `instructions` | はい | システムプロンプトまたは動的 instructions コールバックです。[動的 instructions](#dynamic-instructions) を参照してください。 |
| `prompt` | いいえ | OpenAI Responses API のプロンプト設定です。静的なプロンプトオブジェクトまたは関数を受け取ります。[プロンプトテンプレート](#prompt-templates) を参照してください。 |
| `handoff_description` | いいえ | このエージェントがハンドオフ先として提示される際に公開される短い説明です。 |
| `handoffs` | いいえ | 会話を専門エージェントに委譲します。[handoffs](handoffs.md) を参照してください。 |
| `model` | いいえ | 使用する LLM を指定します。[Models](models/index.md) を参照してください。 |
| `model_settings` | いいえ | `temperature`、`top_p`、`tool_choice` などのモデル調整パラメーターです。 |
| `tools` | いいえ | エージェントが呼び出せるツールです。[Tools](tools.md) を参照してください。 |
| `mcp_servers` | いいえ | エージェント向けの MCP ベースのツールです。[MCP ガイド](mcp.md) を参照してください。 |
| `mcp_config` | いいえ | 厳密なスキーマ変換や MCP 障害フォーマットなど、MCP ツールの準備方法を微調整します。[MCP ガイド](mcp.md#agent-level-mcp-configuration) を参照してください。 |
| `input_guardrails` | いいえ | このエージェントチェーンの最初のユーザー入力で実行されるガードレールです。[Guardrails](guardrails.md) を参照してください。 |
| `output_guardrails` | いいえ | このエージェントの最終出力で実行されるガードレールです。[Guardrails](guardrails.md) を参照してください。 |
| `output_type` | いいえ | プレーンテキストの代わりに構造化された出力型を指定します。[出力型](#output-types) を参照してください。 |
| `hooks` | いいえ | エージェントスコープのライフサイクルコールバックです。[ライフサイクルイベント ( hooks )](#lifecycle-events-hooks) を参照してください。 |
| `tool_use_behavior` | いいえ | ツール結果をモデルに戻すか実行を終了するかを制御します。[ツール使用動作](#tool-use-behavior) を参照してください。 |
| `reset_tool_choice` | いいえ | ツール使用ループを避けるため、ツール呼び出し後に `tool_choice` をリセットします ( 既定値: `True` )。[ツール使用の強制](#forcing-tool-use) を参照してください。 |

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

使用するには、次の手順に従ってください。

1. https://platform.openai.com/playground/prompts に移動します。
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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入ツールです。これは、作成して `Runner.run()` に渡すオブジェクトであり、すべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行の依存関係と状態をまとめる入れ物として機能します。コンテキストには任意の Python オブジェクトを渡せます。

`RunContextWrapper` の完全な仕様、共有使用量トラッキング、ネストされた `tool_input`、シリアライズ時の注意点については、[context ガイド](context.md) を参照してください。

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

既定では、エージェントはプレーンテキスト ( つまり `str` ) を出力します。エージェントに特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトが使われますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型であれば、dataclasses、lists、TypedDict など任意の型をサポートしています。

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

## マルチエージェントシステムの設計パターン

マルチエージェントシステムの設計方法は多数ありますが、広く適用可能なパターンとして一般的に次の 2 つがあります。

1. Manager ( Agents as tools ): 中央の manager / orchestrator が、ツールとして専門サブエージェントを呼び出し、会話の制御を保持します。
2. Handoffs: 同等のエージェント同士が、会話を引き継ぐ専門エージェントへ制御をハンドオフします。これは分散型です。

詳細は [エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### Manager ( Agents as tools )

`customer_facing_agent` はすべてのユーザー対話を処理し、ツールとして公開された専門サブエージェントを呼び出します。詳しくは [tools](tools.md#agents-as-tools) のドキュメントを参照してください。

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

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフが発生すると、委譲先エージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに特化して高い性能を発揮する、モジュール化された専門エージェントが実現できます。詳しくは [handoffs](handoffs.md) のドキュメントを参照してください。

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

ほとんどの場合、エージェント作成時に instructions を指定できます。ただし、関数を介して動的 instructions を指定することもできます。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が使用できます。

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

場合によっては、エージェントのライフサイクルを観測したいことがあります。たとえば、イベントをログに記録したり、データを事前取得したり、特定イベント発生時の使用状況を記録したりできます。

hook のスコープは 2 つあります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他エージェントへのハンドオフを含む `Runner.run(...)` 呼び出し全体を観測します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は `agent.hooks` を介して特定のエージェントインスタンスにアタッチされます。

また、コールバックコンテキストはイベントに応じて変わります。

-   エージェント開始 / 終了 hook は [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。これは元のコンテキストをラップし、共有された実行使用量状態を保持します。
-   LLM、ツール、ハンドオフ hook は [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

一般的な hook のタイミング:

-   `on_agent_start` / `on_agent_end`: 特定エージェントが最終出力の生成を開始 / 終了したとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前 / 直後。
-   `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。
-   `on_handoff`: 制御があるエージェントから別のエージェントへ移るとき。

ワークフロー全体を単一の観測者で扱いたい場合は `RunHooks` を、特定エージェントにカスタム副作用が必要な場合は `AgentHooks` を使用してください。

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

コールバック仕様の全体は、[Lifecycle API リファレンス](ref/lifecycle.md) を参照してください。

## ガードレール

ガードレールを使用すると、エージェント実行と並行してユーザー入力に対するチェック / バリデーションを実行し、さらに生成後のエージェント出力に対してもチェック / バリデーションを実行できます。たとえば、ユーザー入力とエージェント出力の関連性をスクリーニングできます。詳しくは [guardrails](guardrails.md) のドキュメントを参照してください。

## エージェントの複製 / コピー

エージェントで `clone()` メソッドを使用すると、Agent を複製し、必要に応じて任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.4",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールのリストを指定しても、LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM がツールを使うかどうかを判断できます。
2. `required`: LLM にツール使用を必須化します ( ただし、どのツールを使うかは適切に判断できます )。
3. `none`: LLM がツールを _使用しない_ ことを必須化します。
4. 特定の文字列 ( 例: `my_tool` ) を設定: LLM にその特定ツールの使用を必須化します。

OpenAI Responses のツール検索を使用する場合、名前付きツール選択にはより厳しい制限があります。`tool_choice` で素の namespace 名や deferred 専用ツールを指定することはできず、`tool_choice="tool_search"` は [`ToolSearchTool`][agents.tool.ToolSearchTool] を対象にしません。これらの場合は `auto` または `required` を推奨します。Responses 固有の制約については [Hosted tool search](tools.md#hosted-tool-search) を参照してください。

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

`Agent` 設定内の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します。

- `"run_llm_again"`: 既定値です。ツールを実行し、LLM が結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、そのまま最終応答として使用し、以降の LLM 処理は行いません。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツール結果が LLM に送られ、その後 `tool_choice` により別のツール呼び出しが生成される、という流れが無限に続くことで発生します。