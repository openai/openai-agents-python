---
search:
  exclude: true
---
# エージェント

エージェントは、アプリの中核となるビルディングブロックです。エージェントは、 instructions とツールで構成された大規模言語モデル  LLM です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

-   `name`: エージェントを識別する必須の文字列  
-   `instructions`: developer メッセージ、または system prompt とも呼ばれます  
-   `model`: 使用する  LLM と、temperature や top_p などのモデルチューニングパラメーターを指定するオプションの `model_settings`  
-   `tools`: エージェントがタスク達成のために使用できるツール

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
     """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## コンテキスト

エージェントは `context` 型に対してジェネリックです。 context は依存性インジェクション用のツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行時の依存関係と状態の入れ物として機能します。 context には任意の Python オブジェクトを指定できます。

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

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) を出力します。特定の型の出力をエージェントに生成させたい場合は、 `output_type` パラメーターを使用します。一般的な選択肢は [Pydantic](https://docs.pydantic.dev/) オブジェクトですが、 Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型 (dataclass、list、TypedDict など) であればサポートされます。

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

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡すことで、エージェントは関連がある場合にそれらへ委任を選択できます。これは、単一タスクに特化したモジュール化されたエージェントをオーケストレーションできる強力なパターンです。詳しくは [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を指定できますが、関数を介して動的に instructions を提供することも可能です。この関数はエージェントと context を受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方を受け付けます。

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

## ライフサイクル イベント (hooks)

エージェントのライフサイクルを観察したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。 `hooks` プロパティを使ってエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対するチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をチェックすることが可能です。詳しくは [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントのクローン / コピー

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

ツールのリストを渡しても、  LLM  が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto` :  LLM  がツールを使用するかどうかを判断します。  
2. `required` :  LLM  にツール使用を必須にします (どのツールを使うかはインテリジェントに選択)。  
3. `none` :  LLM  にツールを使用しないことを要求します。  
4. 具体的な文字列 (例: `my_tool`) を指定すると、その特定のツールを  LLM  に使用させます。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループの原因は、ツールの結果が  LLM  に送信され、その結果 `tool_choice` により再度ツール呼び出しが生成される、という循環にあります。

    ツール呼び出し後に (auto モードで続行せず) エージェントを完全に停止させたい場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定してください。これにより、ツールの出力をそのまま最終応答として使用し、追加の  LLM  処理を行いません。