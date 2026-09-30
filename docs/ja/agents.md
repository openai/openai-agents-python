---
search:
  exclude: true
---
# エージェント

エージェントは、アプリの中核となる構成要素です。エージェントは、指示、ツール、およびハンドオフ、ガードレール、structured outputs などのオプションのランタイム動作を設定した大規模言語モデル (LLM) です。

`SandboxAgent` ではなく、単一の基本 `Agent` を定義またはカスタマイズする場合は、このページを使用してください。複数のエージェントをどのように連携させるかを決める場合は、[エージェントオーケストレーション](multi_agent.md)を参照してください。マニフェストで定義されたファイルとサンドボックスネイティブの機能を備えた分離ワークスペース内でエージェントを実行する場合は、[サンドボックスエージェントの概念](sandbox/guide.md)を参照してください。

SDK は、OpenAI モデルに対してデフォルトで Responses API を使用しますが、ここでの違いはオーケストレーションにあります。`Agent` と `Runner` を組み合わせることで、SDK がターン、ツール、ガードレール、ハンドオフ、セッションを管理します。このループを自分で管理したい場合は、代わりに Responses API を直接使用してください。

## 次のガイドの選択 {#choose-the-next-guide}

このページを、エージェント定義のハブとして使用してください。次に行う必要がある判断に合った関連ガイドに進んでください。

| 目的 | 次に読むガイド |
| --- | --- |
| モデルまたはプロバイダーの設定を選択する | [モデル](models/index.md) |
| エージェントに機能を追加する | [ツール](tools.md) |
| 実際のリポジトリ、ドキュメント一式、または分離ワークスペースに対してエージェントを実行する | [サンドボックスエージェントのクイックスタート](sandbox_agents.md) |
| マネージャー形式のオーケストレーションとハンドオフのどちらを使用するか決める | [エージェントオーケストレーション](multi_agent.md) |
| ハンドオフの動作を設定する | [ハンドオフ](handoffs.md) |
| ターンの実行、イベントのストリーミング、または会話状態の管理を行う | [エージェントの実行](running_agents.md) |
| 最終出力、実行項目、または再開可能な状態を確認する | [実行結果](results.md) |
| ローカルの依存関係とランタイム状態を共有する | [コンテキスト管理](context.md) |

## 基本設定 {#basic-configuration}

エージェントで最も一般的なプロパティは次のとおりです。

| プロパティ | 必須 | 説明 |
| --- | --- | --- |
| `name` | はい | 人が読める形式のエージェント名です。 |
| `instructions` | いいえ | システムプロンプトまたは動的な指示のコールバックです。使用を強く推奨します。[動的な指示](#dynamic-instructions)を参照してください。 |
| `prompt` | いいえ | OpenAI Responses API のプロンプト設定です。静的なプロンプトオブジェクトまたは関数を受け取ります。[プロンプトテンプレート](#prompt-templates)を参照してください。 |
| `handoff_description` | いいえ | このエージェントがハンドオフ先として提示される際に表示される短い説明です。 |
| `handoffs` | いいえ | 会話を専門エージェントに委任します。[ハンドオフ](handoffs.md)を参照してください。 |
| `model` | いいえ | 使用する LLM です。[モデル](models/index.md)を参照してください。 |
| `model_settings` | いいえ | `temperature`、`top_p`、`tool_choice` などのモデル調整パラメーターです。 |
| `tools` | いいえ | エージェントが呼び出せるツールです。[ツール](tools.md)を参照してください。 |
| `mcp_servers` | いいえ | MCP ベースのツールをエージェントに提供する MCP サーバーです。[MCP ガイド](mcp.md)を参照してください。 |
| `mcp_config` | いいえ | スキーマの strict モードへの変換や MCP エラーの書式設定など、MCP ツールの準備方法を詳細に調整します。[MCP ガイド](mcp.md#agent-level-mcp-configuration)を参照してください。 |
| `input_guardrails` | いいえ | このエージェントチェーンへの最初のユーザー入力に対して実行されるガードレールです。[ガードレール](guardrails.md)を参照してください。 |
| `output_guardrails` | いいえ | このエージェントの最終出力に対して実行されるガードレールです。[ガードレール](guardrails.md)を参照してください。 |
| `output_type` | いいえ | プレーンテキストの代わりに使用する構造化された出力型です。[出力型](#output-types)を参照してください。 |
| `hooks` | いいえ | エージェント単位のライフサイクルコールバックです。[ライフサイクルイベント (フック)](#lifecycle-events-hooks)を参照してください。 |
| `tool_use_behavior` | いいえ | ツールの実行結果をモデルに戻すか、実行を終了するかを制御します。[ツール使用時の動作](#tool-use-behavior)を参照してください。 |
| `reset_tool_choice` | いいえ | ツール使用のループを回避するため、ツール呼び出し後に `tool_choice` をリセットします (デフォルト: `True`)。[ツール使用の強制](#forcing-tool-use)を参照してください。 |

```python
from agents import Agent
from agents.decorators import tool

@tool
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

このセクションの内容はすべて `Agent` に適用されます。`SandboxAgent` は同じ考え方を基盤とし、さらにワークスペース単位の実行用に `default_manifest`、`base_instructions`、`capabilities`、`run_as` を追加します。[サンドボックスエージェントの概念](sandbox/guide.md)を参照してください。

## プロンプトテンプレート {#prompt-templates}

`prompt` を設定すると、OpenAI プラットフォームで作成したプロンプトテンプレートを参照できます。これは、Responses API を介して OpenAI モデルにアクセスする場合に機能します。

使用するには、次の手順を行ってください。

1. https://platform.openai.com/playground/prompts に移動します
2. 新しいプロンプト変数 `poem_style` を作成します。
3. 次の内容でシステムプロンプトを作成します。

    ```
    Write a poem in {{poem_style}}
    ```

4. `--prompt-id` フラグを指定してコード例を実行します。

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

## コンテキスト {#context}

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールです。コンテキストは、自分で作成して `Runner.run()` に渡すオブジェクトであり、すべてのエージェント、ツール、ハンドオフなどに渡されます。また、エージェント実行に必要な依存関係や状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして指定できます。

`RunContextWrapper` の全機能、共有される使用量の追跡、ネストされた `tool_input`、シリアライズに関する注意事項については、[コンテキストガイド](context.md)を参照してください。

```python
from dataclasses import dataclass

@dataclass
class Purchase:
    id: str

@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases(self) -> list[Purchase]:
        # implement your logic here
        return []

agent = Agent[UserContext](
    ...,
)
```

## 出力型 {#output-types}

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) の出力を生成します。エージェントに特定の型の出力を生成させる場合は、`output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意の型をサポートしています。たとえば、データクラス、リスト、TypedDict などです。

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

## マルチエージェントシステムの設計パターン {#multi-agent-system-design-patterns}

マルチエージェントシステムを設計する方法は多数ありますが、一般的に幅広く適用できる次の 2 つのパターンがよく見られます。

1. マネージャー (agents as tools): 中央のマネージャー／オーケストレーターが、専門のサブエージェントをツールとして呼び出し、会話の制御を維持します。
2. ハンドオフ: 同等の立場にあるエージェントが、会話を引き継ぐ専門エージェントへ制御をハンドオフします。これは分散型のパターンです。

詳細については、[エージェント構築の実践ガイド](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)を参照してください。

### マネージャー (agents as tools) {#manager-agents-as-tools}

`customer_facing_agent` はすべてのユーザー操作を処理し、ツールとして公開された専門のサブエージェントを呼び出します。詳細については、[ツール](tools.md#agents-as-tools)のドキュメントを参照してください。

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

### ハンドオフ {#handoffs}

設定されたハンドオフ先は、エージェントが処理を委任できるサブエージェントです。ハンドオフが発生すると、委任先のエージェントが会話履歴を受け取り、会話を引き継ぎます。このパターンにより、単一のタスクに優れたモジュール式の専門エージェントを構築できます。詳細については、[ハンドオフ](handoffs.md)のドキュメントを参照してください。

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

## 動的な指示 {#dynamic-instructions}

ほとんどの場合、エージェントの作成時に指示を指定できます。ただし、関数を介して動的な指示を指定することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方を使用できます。

```python
from agents import Agent, RunContextWrapper

def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## ライフサイクルイベント (フック) {#lifecycle-events-hooks}

エージェントのライフサイクルを監視したい場合があります。たとえば、特定のイベントが発生したときに、イベントのログ記録、データの事前取得、使用量の記録を行いたい場合があります。

フックには次の 2 つのスコープがあります。

-   [`RunHooks`][agents.lifecycle.RunHooks] は、他のエージェントへのハンドオフを含む `Runner.run(...)` 呼び出し全体を監視します。
-   [`AgentHooks`][agents.lifecycle.AgentHooks] は、`agent.hooks` を介して特定のエージェントインスタンスに関連付けられます。

コールバックのコンテキストも、イベントによって異なります。

-   エージェントの開始／終了フックは [`AgentHookContext`][agents.run_context.AgentHookContext] を受け取ります。これは元のコンテキストをラップし、共有される実行使用量の状態を保持します。
-   LLM、ツール、ハンドオフのフックは [`RunContextWrapper`][agents.run_context.RunContextWrapper] を受け取ります。

一般的なフックのタイミングは次のとおりです。

-   `on_agent_start`: 特定のエージェントが実行を開始したとき。`on_agent_end`: そのエージェントが最終出力の生成を完了したとき。
-   `on_llm_start` / `on_llm_end`: 各モデル呼び出しの直前／直後。
- `on_tool_start` / `on_tool_end`: 各ローカルツール呼び出しの前後。関数ツールの場合、フックの `context` は通常 `ToolContext` であるため、`tool_call_id` などのツール呼び出しメタデータを確認できます。
-   `on_handoff`: 制御があるエージェントから別のエージェントに移ったとき。

ワークフロー全体を 1 つのオブザーバーで監視する場合は `RunHooks` を使用し、特定のエージェントに限定したライフサイクルコールバックが必要な場合は `AgentHooks` を使用します。

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

コールバックの全機能については、[Lifecycle API リファレンス](ref/lifecycle.md)を参照してください。

## ガードレール {#guardrails}

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対するチェック／検証を実行し、エージェントの出力が生成された後にその出力をチェックできます。たとえば、ユーザー入力とエージェント出力が関連性のある内容かどうかを審査できます。詳細については、[ガードレール](guardrails.md)のドキュメントを参照してください。

## エージェントの複製／コピー {#cloningcopying-agents}

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="gpt-5.6-sol",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

`clone()` は `dataclasses.replace` を使用するため、シャローコピーを実行します。`tools`、`handoffs`、`mcp_servers`、`input_guardrails`、`output_guardrails` など、上書きしないリスト属性は、元のエージェントが保持するものとまったく同じリストのままです。したがって、どちらかのエージェントを介してそのリストを変更すると、両方のエージェントに影響します。クローンに独立したリストコンテナーを持たせるには、たとえば `pirate_agent.clone(tools=[*pirate_agent.tools, extra_tool])` のように新しいリストを渡します。その新しいリストにコピーされた項目は、それらの項目も置き換えない限り、同じツールまたはハンドオフオブジェクトのままです。

## ツール使用の強制 {#forcing-tool-use}

ツールのリストを指定しても、LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツールの使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使用するかどうかを LLM が判断できます。
2. `required`: LLM にツールの使用を必須としますが、使用するツールは LLM が適切に判断できます。
3. `none`: LLM にツールを使用 _させない_ ようにします。
4. `my_tool` などの特定の文字列を設定すると、LLM にその特定のツールの使用を必須とします。

OpenAI Responses のツール検索を使用する場合、名前を指定したツール選択には、より多くの制限があります。`tool_choice` では、単独の名前空間名や遅延専用ツールを指定できません。また、`tool_choice="tool_search"` では [`ToolSearchTool`][agents.tool.ToolSearchTool] を指定できません。その場合は、`auto` または `required` の使用を推奨します。Responses 固有の制約については、[ホスト型ツール検索](tools.md#hosted-tool-search)を参照してください。

```python
from agents import Agent, ModelSettings
from agents.decorators import tool

@tool
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

## ツール使用時の動作 {#tool-use-behavior}

`Agent` 設定の `tool_use_behavior` パラメーターは、ツール出力の処理方法を制御します。

- `"run_llm_again"`: デフォルトです。ツールが実行され、LLM が実行結果を処理して最終応答を生成します。
- `"stop_on_first_tool"`: 最初のツール呼び出しの出力を、LLM で追加処理せずに最終応答として使用します。

```python
from agents import Agent
from agents.decorators import tool

@tool
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
from agents import Agent
from agents.agent import StopAtTools
from agents.decorators import tool

@tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@tool
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

- `ToolsToFinalOutputFunction`: ツールの実行結果を処理し、最終出力で実行を終了するか、LLM による処理を続行するかを決定するカスタム関数です。

```python
from agents import Agent, FunctionToolResult, RunContextWrapper
from agents.agent import ToolsToFinalOutputResult
from agents.decorators import tool
from typing import List, Any

@tool
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

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に「auto」にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループが発生するのは、ツールの実行結果が LLM に送信され、その後 `tool_choice` によって LLM が別のツール呼び出しを生成し、この処理が無限に繰り返されるためです。