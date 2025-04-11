# エージェント

エージェントはアプリのコア構成要素です。エージェントは、指示とツールで構成された大規模言語モデル (LLM) です。

## 基本設定

エージェントで設定する最も一般的なプロパティは次のとおりです：

-   `instructions`: 開発者メッセージまたはシステムプロンプトとも呼ばれます。
-   `model`: 使用する LLM と、temperature や top_p などのモデル調整パラメーターを設定するためのオプションの `model_settings`。
-   `tools`: エージェントがタスクを達成するために使用できるツール。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## コンテキスト

エージェントは `context` タイプに対して汎用的です。コンテキストは依存性注入ツールであり、`Runner.run()` に渡すために作成するオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェントの実行に必要な依存関係や状態をまとめたものとして機能します。任意の Python オブジェクトをコンテキストとして提供できます。

```python
@dataclass
class UserContext:
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 出力タイプ

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）の出力を生成します。特定のタイプの出力をエージェントに生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢として [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる任意のタイプをサポートしています - データクラス、リスト、TypedDict など。

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

    `output_type` を渡すと、モデルに通常のプレーンテキスト応答の代わりに [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示します。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを提供し、エージェントは関連する場合にそれらに委任することができます。これは、単一のタスクに優れたモジュール化された専門エージェントを編成する強力なパターンです。[handoffs](handoffs.md) ドキュメントで詳細をお読みください。

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

## 動的指示

ほとんどの場合、エージェントを作成する際に指示を提供できますが、関数を介して動的な指示を提供することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が受け入れられます。

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

時には、エージェントのライフサイクルを観察したいことがあります。たとえば、イベントを記録したり、特定のイベントが発生したときにデータを事前取得したりすることができます。`hooks` プロパティを使用してエージェントのライフサイクルにフックすることができます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、興味のあるメソッドをオーバーライドします。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力のチェック/検証を行うことができます。たとえば、ユーザーの入力を関連性でスクリーニングすることができます。[guardrails](guardrails.md) ドキュメントで詳細をお読みください。

## エージェントのクローン/コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、任意のプロパティを変更することができます。

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

ツールのリストを提供しても、LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです：

1. `auto`：LLM がツールを使用するかどうかを決定します。
2. `required`：LLM がツールを使用する必要があります（ただし、どのツールを使用するかは賢く決定できます）。
3. `none`：LLM がツールを使用しないことを要求します。
4. 特定の文字列を設定する例：`my_tool`、特定のツールを使用することを要求します。

!!! note

    無限ループを防ぐために、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツールの結果が LLM に送信され、その後 `tool_choice` によって別のツール呼び出しが生成されるために発生します。

    ツール呼び出し後にエージェントが完全に停止するようにしたい場合（自動モードで続行するのではなく）、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定することで、ツールの出力を最終応答として直接使用し、さらなる LLM 処理を行わないようにできます。