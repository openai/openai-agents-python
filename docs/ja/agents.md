---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核の構成要素です。エージェントとは、 instructions と tools で設定された大規模言語モデル（ LLM ）です。

## 基本設定

エージェントで最もよく設定するプロパティは次のとおりです。

-   `name`: エージェントを識別する必須の文字列。
-   `instructions`: 開発者メッセージまたは system prompt とも呼ばれます。
-   `model`: 使用する LLM と、 temperature や top_p などのモデル調整パラメーターを指定する省略可能な `model_settings`。
-   `tools`: エージェントがタスクを遂行するために利用できるツールの一覧。

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

エージェントは汎用的に `context` 型を取ります。コンテキストは依存性インジェクション用ツールであり、あなたが作成して `Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行時の依存関係や状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして渡せます。

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

既定では、エージェントはプレーンテキスト（つまり `str` ）を出力します。エージェントに特定の型の出力を生成させたい場合は、 `output_type` パラメーターを使用します。よく使われるのは [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップ可能な型 ― dataclass、リスト、TypedDict など ― であれば何でもサポートしています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡すと、関連性がある場合にエージェントがそれらへ委任できます。これは、単一タスクに特化したモジュール型エージェントをオーケストレーションする強力なパターンです。詳細は [ハンドオフ](handoffs.md) ドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を指定できますが、関数を介して動的に渡すことも可能です。その関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数も `async` 関数も使用できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりしたい場合です。 `hooks` プロパティを使ってエージェントのライフサイクルにフックできます。 [`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力のチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をスクリーニングすることが可能です。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを渡しても、必ずしも LLM がツールを使用するとは限りません。 [`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は以下のとおりです。

1. `auto` : LLM がツールを使用するかどうかを判断します。
2. `required` : LLM にツール使用を必須にします（ただし使用するツールは自動選択）。
3. `none` : LLM にツールを使用しないことを必須にします。
4. 文字列（例 `my_tool` ）を指定すると、その特定のツールを必ず使用させます。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が再度 LLM に送られ、 `tool_choice` により再びツール呼び出しが生成される、という繰り返しが原因です。

    ツール呼び出し後にエージェントを完全に停止させたい場合（自動モードを続行しない）、 [`Agent.tool_use_behavior="stop_on_first_tool"`] を設定できます。この場合、ツールの出力をそのまま最終応答として使用し、追加の LLM 処理は行いません。