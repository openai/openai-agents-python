---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核的な基本構成要素です。エージェントは、instructions、tools、および handoffs、ガードレール、structured outputs などの任意の実行時動作で構成された大規模言語モデル (LLM) です。

単一のプレーンな `Agent` を定義またはカスタマイズしたい場合は、このページを使用してください。複数のエージェントをどのように連携させるかを決める場合は、[エージェントオーケストレーション](multi_agent.md) を参照してください。manifest で定義されたファイルと sandbox ネイティブの機能を備えた分離ワークスペース内でエージェントを実行する必要がある場合は、[Sandbox agent concepts](sandbox/guide.md) を参照してください。

SDK は OpenAI モデルに対してデフォルトで Responses API を使用しますが、ここでの違いはオーケストレーションです。`Agent` と `Runner` により、SDK がターン、tools、ガードレール、ハンドオフ、セッションを管理します。このループを自分で管理したい場合は、代わりに Responses API を直接使用してください。

## 次ガイドの選択

このページはエージェント定義のハブとして使用してください。次に必要な判断に合う隣接ガイドへ移動してください。

| 目的 | 次に読むもの |
| --- | --- |
| モデルまたは provider の設定を選ぶ | [Models](models/index.md) |
| エージェントに機能を追加する | [Tools](tools.md) |
| 実際の repo、ドキュメントバンドル、または分離ワークスペースでエージェントを実行する | [Sandbox agents quickstart](sandbox_agents.md) |
| manager スタイルのオーケストレーションとハンドオフのどちらにするか決める | [エージェントオーケストレーション](multi_agent.md) |
| ハンドオフ動作を設定する | [Handoffs](handoffs.md) |
| ターン実行、イベントのストリーミング、会話状態の管理を行う | [Running agents](running_agents.md) |
| 最終出力、実行項目、再開可能な状態を確認する | [Results](results.md) |
| ローカル依存関係と実行時状態を共有する | [Context management](context.md) |

## 基本設定

エージェントの最も一般的なプロパティは次のとおりです。

| プロパティ | 必須 | 説明 |
| --- | --- | --- |
| `name` | yes | 人間が読めるエージェント名。 |
| `instructions` | yes | システムプロンプトまたは動的 instructions コールバック。[動的 instructions](#dynamic-instructions) を参照してください。 |
| `prompt` | no | OpenAI Responses API の prompt 設定。静的な prompt オブジェクトまたは関数を受け取ります。[プロンプトテンプレート](#prompt-templates) を参照してください。 |
| `handoff_description` | no | このエージェントがハンドオフ先として提示される際に公開される短い説明。 |
| `handoffs` | no | 会話を専門エージェントに委譲します。[handoffs](handoffs.md) を参照してください。 |
| `model` | no | 使用する LLM。[Models](models/index.md) を参照してください。 |
| `model_settings` | no | `temperature`、`top_p`、`tool_choice` などのモデル調整パラメーター。 |
| `tools` | no | エージェントが呼び出せるツール。[Tools](tools.md) を参照してください。 |
| `mcp_servers` | no | エージェント向けの MCP ベースのツール。[MCP ガイド](mcp.md) を参照してください。 |
| `mcp_config` | no | strict な schema 変換や MCP 障害フォーマットなど、MCP ツールの準備方法を微調整します。[MCP ガイド](mcp.md#agent-level-mcp-configuration) を参照してください。 |
| `input_guardrails` | no | このエージェントチェーンの最初のユーザー入力で実行されるガードレール。[Guardrails](guardrails.md) を参照してください。 |
| `output_guardrails` | no | このエージェントの最終出力で実行されるガードレール。[Guardrails](guardrails.md) を参照してください。 |
| `output_type` | no | プレーンテキストの代わりに構造化出力型を使用します。[出力型](#output-types) を参照してください。 |
| `hooks` | no | エージェントスコープのライフサイクルコールバック。[ライフサイクルイベント (hooks)](#lifecycle-events-hooks) を参照してください。 |
| `tool_use_behavior` | no | ツール結果をモデルに戻すか実行を終了するかを制御します。[ツール使用動作](#tool-use-behavior) を参照してください。 |
| `reset_tool_choice` | no | ツール使用ループを避けるため、ツール呼び出し後に `tool_choice` をリセットします (デフォルト: `True`)。[ツール使用の強制](#forcing-tool-use) を参照してください。 |

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

このセクションの内容はすべて `Agent` に適用されます。`SandboxAgent` は同じ考え方を基にしつつ、ワークスペーススコープの実行向けに `default_manifest`、`base_instructions`、`capabilities`、`run_as` を追加します。[Sandbox agent concepts](sandbox/guide.md) を参照してください。

## プロンプトテンプレート

`prompt` を設定することで、OpenAI platform で作成したプロンプトテンプレートを参照できます。これは Responses API を使用する OpenAI モデルで動作します。

使用するには、次を行ってください。

1. https://platform.openai.com/playground/prompts に移動します
2. 新しい prompt 変数 `poem_style` を作成します。
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

実行時にプロンプトを動的生成することもできます。

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

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入ツールです。これは `Runner.run()` に渡すために作成するオブジェクトで、すべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行の依存関係と状態をまとめて保持する入れ物として機能します。任意の Python オブジェクトをコンテキストとして提供できます。

完全な `RunContextWrapper` の仕様、共有使用量トラッキング、ネストした `tool_input`、シリアライズ上の注意点については、[context ガイド](context.md) を参照してください。

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

デフォルトでは、エージェントはプレーンテキスト (すなわち `str`) の出力を生成します。エージェントに特定の型の出力を生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な型であれば、dataclasses、lists、TypedDict など任意の型をサポートします。

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

マルチエージェントシステムの設計方法は多数ありますが、広く適用できるパターンとして次の 2 つをよく見かけます。

1. Manager (Agents as tools): 中央の manager / orchestrator が specialized sub-agents をツールとして呼び出し、会話の制御を保持します。
2. Handoffs: 対等なエージェントが会話を引き継ぐ specialized agent に制御をハンドオフします。これは分散型です。

詳細は [our practical guide to building agents](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf) を参照してください。

### Manager (Agents as tools)

`customer_facing_agent` はすべてのユーザー対話を処理し、ツールとして公開された specialized sub-agents を呼び出します。詳細は [tools](tools.md#agents-as-tools) ドキュメントを参照してください。

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

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフが発生すると、委譲先エージェントは会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一タスクに優れたモジュール型の専門エージェントを実現できます。詳細は [handoffs](handoffs.md) ドキュメントを参照してください。

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

ほとんどの場合、エージェント作成時に instructions を指定できます。ただし、関数を介して動的 instructions を指定することもできます。関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が受け入れられます。

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

エージェントのライフサイクルを観測したい場合があります。たとえば、イベントのログ記録、データの事前取得、特定イベント発生時の使用量記録などを行いたいことがあります。

hook には 2 つのスコープがあります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他エージェントへのハンドオフを含む `Runner.run(...)` 呼び出し全体を観測します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は `agent.hooks` を通じて特定のエージェントインスタンスにアタッチされます。

コールバックコンテキストもイベントに応じて変化します。

-   エージェント開始 / 終了 hook は [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。これは元のコンテキストをラップし、共有の実行使用量状態を保持します。
-   LLM、ツール、ハンドオフ hook は [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

典型的な hook タイミング:

-   `on_agent_start` / `on_agent_end`: 特定エージェントが最終出力の生成を開始 / 終了したとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前 / 直後。
-   `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。
-   `on_handoff`: 制御があるエージェントから別のエージェントへ移るとき。

ワークフロー全体を 1 つの観測者で見たい場合は `RunHooks` を、特定エージェントにカスタム副作用が必要な場合は `AgentHooks` を使用してください。

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

コールバック仕様の全体は [Lifecycle API reference](ref/lifecycle.md) を参照してください。

## ガードレール

ガードレールを使うと、エージェント実行と並行してユーザー入力に対するチェック / 検証を実行し、さらに生成後のエージェント出力に対しても実行できます。たとえば、ユーザー入力とエージェント出力の関連性を審査できます。詳細は [guardrails](guardrails.md) ドキュメントを参照してください。

## エージェントの複製 / コピー

エージェントの `clone()` メソッドを使用すると、Agent を複製し、必要に応じて任意のプロパティを変更できます。

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

ツールのリストを指定しても、LLM が必ずツールを使うとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: LLM がツールを使うかどうかを決定できます。
2. `required`: LLM にツール使用を必須にします (ただしどのツールを使うかは賢く判断できます)。
3. `none`: LLM にツールを _使わない_ ことを必須にします。
4. 特定の文字列 (例: `my_tool`) を設定: LLM にその特定ツールの使用を必須にします。

OpenAI Responses tool search を使用している場合、名前付き tool choice はさらに制限されます。`tool_choice` で bare namespace 名や deferred-only ツールを対象にできず、`tool_choice="tool_search"` は [`ToolSearchTool`][agents.tool.ToolSearchTool] を対象にしません。これらの場合は `auto` または `required` を推奨します。Responses 固有の制約については [Hosted tool search](tools.md#hosted-tool-search) を参照してください。

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

`Agent` 設定の `tool_use_behavior` パラメーターは、ツール出力の処理方法を制御します。

- `"run_llm_again"`: デフォルト。ツールが実行され、LLM が結果を処理して最終応答を生成します。
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

- `ToolsToFinalOutputFunction`: ツール結果を処理し、停止するか LLM で継続するかを判断するカスタム関数です。

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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループが起こる理由は、ツール結果が LLM に送信され、`tool_choice` のために LLM がさらに別のツール呼び出しを生成し、これが際限なく続くためです。