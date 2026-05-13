---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核的な構成要素です。エージェントとは、instructions、ツール、およびハンドオフ、ガードレール、structured outputs などの任意のランタイム動作で設定された大規模言語モデル (LLM) です。

単一のシンプルな `Agent` を定義またはカスタマイズしたい場合は、このページを使用してください。複数のエージェントをどのように連携させるかを検討している場合は、[エージェントオーケストレーション](multi_agent.md) を読んでください。エージェントを、マニフェストで定義されたファイルとサンドボックスネイティブ機能を備えた分離ワークスペース内で実行する必要がある場合は、[Sandbox エージェントの概念](sandbox/guide.md) を読んでください。

SDK は OpenAI モデルに対してデフォルトで Responses API を使用しますが、ここでの違いはオーケストレーションです。`Agent` と `Runner` により、SDK がターン、ツール、ガードレール、ハンドオフ、セッションを管理できます。そのループを自分で管理したい場合は、代わりに Responses API を直接使用してください。

## 次のガイドの選択

このページをエージェント定義のハブとして使用してください。次に行う判断に合ったガイドへ進んでください。

| したいこと | 次に読むもの |
| --- | --- |
| モデルまたはプロバイダー設定を選択する | [モデル](models/index.md) |
| エージェントに機能を追加する | [ツール](tools.md) |
| 実際のリポジトリ、ドキュメントバンドル、または分離ワークスペースに対してエージェントを実行する | [Sandbox エージェントのクイックスタート](sandbox_agents.md) |
| マネージャースタイルのオーケストレーションとハンドオフのどちらにするかを決める | [エージェントオーケストレーション](multi_agent.md) |
| ハンドオフ動作を設定する | [ハンドオフ](handoffs.md) |
| ターンを実行し、イベントをストリーミングし、会話状態を管理する | [エージェント実行](running_agents.md) |
| 最終出力、実行アイテム、または再開可能な状態を確認する | [結果](results.md) |
| ローカル依存関係とランタイム状態を共有する | [コンテキスト管理](context.md) |

## 基本設定

エージェントで最も一般的なプロパティは次のとおりです。

| プロパティ | 必須 | 説明 |
| --- | --- | --- |
| `name` | はい | 人間が読みやすいエージェント名。 |
| `instructions` | いいえ | システムプロンプトまたは動的 instructions コールバック。強く推奨します。[動的 instructions](#dynamic-instructions) を参照してください。 |
| `prompt` | いいえ | OpenAI Responses API のプロンプト設定。静的なプロンプトオブジェクトまたは関数を受け付けます。[プロンプトテンプレート](#prompt-templates) を参照してください。 |
| `handoff_description` | いいえ | このエージェントがハンドオフ先として提示されるときに公開される短い説明。 |
| `handoffs` | いいえ | 会話を専門エージェントに委任します。[ハンドオフ](handoffs.md) を参照してください。 |
| `model` | いいえ | 使用する LLM。[モデル](models/index.md) を参照してください。 |
| `model_settings` | いいえ | `temperature`、`top_p`、`tool_choice` などのモデル調整パラメーター。 |
| `tools` | いいえ | エージェントが呼び出せるツール。[ツール](tools.md) を参照してください。 |
| `mcp_servers` | いいえ | エージェント向けの MCP ベースのツール。[MCP ガイド](mcp.md) を参照してください。 |
| `mcp_config` | いいえ | 厳格なスキーマ変換や MCP 失敗時の形式設定など、MCP ツールの準備方法を微調整します。[MCP ガイド](mcp.md#agent-level-mcp-configuration) を参照してください。 |
| `input_guardrails` | いいえ | このエージェントチェーンの最初のユーザー入力に対して実行されるガードレール。[ガードレール](guardrails.md) を参照してください。 |
| `output_guardrails` | いいえ | このエージェントの最終出力に対して実行されるガードレール。[ガードレール](guardrails.md) を参照してください。 |
| `output_type` | いいえ | プレーンテキストの代わりとなる構造化出力型。[出力型](#output-types) を参照してください。 |
| `hooks` | いいえ | エージェントスコープのライフサイクルコールバック。[ライフサイクルイベント (フック)](#lifecycle-events-hooks) を参照してください。 |
| `tool_use_behavior` | いいえ | ツールの結果をモデルに戻してループさせるか、実行を終了するかを制御します。[ツール使用の動作](#tool-use-behavior) を参照してください。 |
| `reset_tool_choice` | いいえ | ツール呼び出し後に `tool_choice` をリセットします (デフォルト: `True`)。これによりツール使用ループを避けます。[ツール使用の強制](#forcing-tool-use) を参照してください。 |

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

このセクションの内容はすべて `Agent` に適用されます。`SandboxAgent` は同じ考え方を基盤とし、それに `default_manifest`、`base_instructions`、`capabilities`、`run_as` を加えて、ワークスペーススコープの実行に対応します。[Sandbox エージェントの概念](sandbox/guide.md) を参照してください。

## プロンプトテンプレート

`prompt` を設定することで、OpenAI プラットフォームで作成したプロンプトテンプレートを参照できます。これは Responses API を使用する OpenAI モデルで動作します。

使用するには、次の手順を行ってください。

1. https://platform.openai.com/playground/prompts に移動します
2. 新しいプロンプト変数 `poem_style` を作成します。
3. 次の内容でシステムプロンプトを作成します。

    ```
    Write a poem in {{poem_style}}
    ```

4. この例を `--prompt-id` フラグ付きで実行します。

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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールです。自分で作成して `Runner.run()` に渡すオブジェクトであり、すべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行の依存関係や状態をまとめて保持します。コンテキストには任意の Python オブジェクトを指定できます。

完全な `RunContextWrapper` インターフェイス、共有された使用量追跡、ネストした `tool_input`、およびシリアライズ上の注意点については、[コンテキストガイド](context.md) を読んでください。

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

デフォルトでは、エージェントはプレーンテキスト (すなわち `str`) の出力を生成します。エージェントに特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用することですが、Pydantic [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型にも対応しています。dataclasses、lists、TypedDict などです。

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

    `output_type` を渡すと、モデルに通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示することになります。

## マルチエージェントシステムの設計パターン

マルチエージェントシステムを設計する方法は数多くありますが、幅広く適用できるパターンとして、よく見られるものが 2 つあります。

1. マネージャー (agents as tools): 中央のマネージャー/オーケストレーターが、専門化されたサブエージェントをツールとして呼び出し、会話の制御を維持します。
2. ハンドオフ: 対等なエージェントが、会話を引き継ぐ専門エージェントへ制御をハンドオフします。これは分散型です。

詳細は、[エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### マネージャー (agents as tools)

`customer_facing_agent` はユーザーとのやり取りをすべて処理し、ツールとして公開された専門サブエージェントを呼び出します。詳しくは [ツール](tools.md#agents-as-tools) ドキュメントを読んでください。

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

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフが発生すると、委任先のエージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに優れた、モジュール化された専門エージェントを実現できます。詳しくは [ハンドオフ](handoffs.md) ドキュメントを読んでください。

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

ほとんどの場合、エージェントを作成するときに instructions を指定できます。ただし、関数を通じて動的な instructions を提供することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数のどちらも受け付けます。

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

## ライフサイクルイベント (フック)

場合によっては、エージェントのライフサイクルを監視したいことがあります。たとえば、特定のイベントが発生したときに、イベントをログに記録したり、データを事前取得したり、使用量を記録したりできます。

フックのスコープは 2 つあります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他のエージェントへのハンドオフを含む `Runner.run(...)` 呼び出し全体を監視します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は、`agent.hooks` を通じて特定のエージェントインスタンスにアタッチされます。

コールバックのコンテキストもイベントに応じて変わります。

-   エージェントの開始/終了フックは [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。これは元のコンテキストをラップし、共有された実行使用量状態を保持します。
-   LLM、ツール、ハンドオフのフックは [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

典型的なフックのタイミングは次のとおりです。

-   `on_agent_start` / `on_agent_end`: 特定のエージェントが最終出力の生成を開始または終了するとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前/直後。
-   `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。
    関数ツールの場合、フックの `context` は通常 `ToolContext` なので、`tool_call_id` などのツール呼び出しメタデータを確認できます。
-   `on_handoff`: 制御があるエージェントから別のエージェントへ移るとき。

ワークフロー全体に対して単一のオブザーバーが必要な場合は `RunHooks` を使用し、1 つのエージェントにカスタム副作用が必要な場合は `AgentHooks` を使用します。

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

完全なコールバック API については、[ライフサイクル API リファレンス](ref/lifecycle.md) を参照してください。

## ガードレール

ガードレールを使うと、エージェントの実行と並行してユーザー入力に対するチェック/検証を実行し、エージェントの出力が生成された後にその出力に対するチェック/検証を実行できます。たとえば、ユーザー入力とエージェント出力について関連性をスクリーニングできます。詳しくは [ガードレール](guardrails.md) ドキュメントを読んでください。

## エージェントのクローン/コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.5",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールのリストを指定しても、LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM がツールを使用するかどうかを判断できるようにします。
2. `required`: LLM にツールの使用を要求します (ただし、どのツールを使うかは賢く判断できます)。
3. `none`: LLM にツールを使用 _しない_ ことを要求します。
4. `my_tool` などの特定の文字列を設定すると、LLM にその特定のツールを使用することを要求します。

OpenAI Responses のツール検索を使用している場合、名前付きツール選択にはより多くの制限があります。`tool_choice` で名前空間名だけや deferred-only ツールをターゲットにすることはできず、`tool_choice="tool_search"` は [`ToolSearchTool`][agents.tool.ToolSearchTool] をターゲットにしません。このような場合は、`auto` または `required` を優先してください。Responses 固有の制約については、[ホスト型ツール検索](tools.md#hosted-tool-search) を参照してください。

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

## ツール使用の動作

`Agent` 設定の `tool_use_behavior` パラメーターは、ツール出力の扱い方を制御します。

- `"run_llm_again"`: デフォルトです。ツールが実行され、LLM がその結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力が、追加の LLM 処理なしで最終応答として使用されます。

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

- `ToolsToFinalOutputFunction`: ツールの結果を処理し、停止するか、LLM による処理を続行するかを決定するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループが発生するのは、ツールの結果が LLM に送信され、その後 `tool_choice` のために LLM が別のツール呼び出しを生成し、これが際限なく繰り返されるためです。