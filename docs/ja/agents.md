# エージェント

エージェント はアプリケーションの基本的な構成要素です。エージェント は、大規模言語モデル（LLM）であり、 instructions と tools を用いて設定されます。

## 基本設定

エージェント の設定でよく使われるプロパティは以下の通りです。

- `instructions` : 開発者メッセージまたはシステムプロンプトとも呼ばれます。
- `model` : 使用する LLM を指定します。オプションで `model_settings` を指定し、temperature や top_p などのモデル調整パラメータを設定できます。
- `tools` : エージェント がタスクを達成するために使用できるツールです。

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

エージェント は、 `context` 型に対してジェネリックです。コンテキストは依存性注入のためのツールであり、作成したオブジェクトを `Runner.run()` に渡すことで、各エージェント、ツール、ハンドオフなどに渡されます。これはエージェントの実行に必要な依存関係や状態をまとめて保持するためのものです。任意の Python オブジェクトをコンテキストとして提供できます。

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

デフォルトでは、エージェント はプレーンテキスト（つまり `str` ）を出力します。特定の型の出力を生成させたい場合は、 `output_type` パラメータを使用します。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使用しますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な型（データクラス、リスト、TypedDict など）であればどのような型でもサポートしています。

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

    `output_type` を指定すると、モデルは通常のプレーンテキストのレスポンスではなく、 [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用します。

## ハンドオフ

ハンドオフ は、エージェント が処理を委譲できるサブエージェントです。ハンドオフのリストを提供すると、エージェント は必要に応じてそれらに処理を委譲できます。これは、特定のタスクに特化したモジュール型のエージェントを組み合わせて調整するための強力なパターンです。詳細は [ハンドオフ](handoffs.md) のドキュメントを参照してください。

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

## 動的な instructions

多くの場合、エージェント 作成時に instructions を指定しますが、関数を通じて動的に instructions を提供することも可能です。この関数はエージェントとコンテキストを受け取り、プロンプトを返します。通常の関数と `async` 関数の両方が使用可能です。

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

エージェント のライフサイクルを監視したい場合があります。例えば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。エージェント のライフサイクルにフックするには、 `hooks` プロパティを使用します。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドします。

## ガードレール

ガードレール を使用すると、エージェント の実行と並行してユーザー入力に対するチェックや検証を実行できます。例えば、ユーザー入力の関連性を検証できます。詳細は [ガードレール](guardrails.md) のドキュメントを参照してください。

## エージェント の複製（クローン）

エージェント の `clone()` メソッドを使用すると、エージェント を複製し、必要に応じてプロパティを変更できます。

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

ツールのリストを提供しても、必ずしも LLM がツールを使用するとは限りません。ツールの使用を強制するには、 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定します。有効な値は以下の通りです。

1. `auto` : LLM がツールを使用するかどうかを決定します。
2. `required` : LLM にツールの使用を強制します（ただし、どのツールを使用するかは LLM が判断します）。
3. `none` : LLM にツールを使用しないことを強制します。
4. 特定の文字列（例： `my_tool` ）を設定すると、LLM はその特定のツールを使用することを強制されます。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に自動的に `tool_choice` を "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツールの実行結果が LLM に送信され、再びツール呼び出しが生成されるために発生します。

    ツール呼び出し後に完全に停止させたい場合（自動モードで続行しない場合）は、 [`Agent.tool_use_behavior="stop_on_first_tool"`] を設定すると、ツールの出力を最終レスポンスとして直接使用し、それ以上の LLM 処理を行いません。