---
search:
  exclude: true
---
# ガードレール

ガードレールを使うと、ユーザー入力やエージェント出力のチェックや検証ができます。たとえば、カスタマーリクエスト対応のために非常に賢い（つまり遅く/高価な）モデルを使うエージェントがあるとします。悪意ある ユーザー がそのモデルに数学の宿題の手伝いをさせようとするのは避けたいはずです。そこで、速く/安価なモデルでガードレールを実行できます。もしガードレールが不正利用を検出したら、その場でエラーを送出して高価なモデルの実行を防ぎ、時間とコストを節約できます（ **ブロッキング型のガードレールを使用する場合。並列ガードレールの場合は、ガードレールの完了前に高価なモデルの実行が開始されている可能性があります。詳細は下記「実行モード」を参照してください** ）。

ガードレールには次の 2 種類があります。

1. 入力ガードレールは最初の ユーザー 入力で実行されます
2. 出力ガードレールは最終的なエージェント出力で実行されます

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたのと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、適切に ユーザー に応答するか、例外を処理できます。

!!! Note

    入力ガードレールは ユーザー 入力で実行されることを想定しているため、あるエージェントのガードレールが実行されるのは、そのエージェントが最初のエージェントである場合のみです。「なぜ `guardrails` プロパティがエージェント側にあり、`Runner.run` に渡さないのか」と疑問に思うかもしれません。これは、ガードレールが実際の エージェント に密接に関係する傾向があるためです。エージェントごとに異なるガードレールを実行することになるため、コードを同じ場所に置くことで可読性が向上します。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールはエージェントの実行と同時に走ります。両方が同時に開始されるため、レイテンシが最も良好です。ただし、ガードレールが失敗した場合、キャンセルされるまでにエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールはエージェント開始の前に実行され完了します。ガードレールのトリップワイヤーが発火した場合、エージェントは実行されず、トークン消費やツールの実行を防止できます。コスト最適化や、ツール呼び出しによる副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、適切に ユーザー に応答するか、例外を処理できます。

!!! Note

    出力ガードレールは最終的なエージェント出力で実行されることを想定しているため、あるエージェントのガードレールが実行されるのは、そのエージェントが最後のエージェントである場合のみです。入力ガードレールと同様に、ガードレールは実際の エージェント に密接に関係する傾向があるため、コードを同じ場所に置くことで可読性が向上します。

    出力ガードレールは常にエージェントの完了後に実行されるため、`run_in_parallel` パラメーターはサポートしていません。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーでそれを通知できます。トリップワイヤーが発火したガードレールを検出するとすぐに、{Input,Output}GuardrailTripwireTriggered 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部で エージェント を実行して行います。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)

class MathHomeworkOutput(BaseModel):
    is_math_homework: bool
    reasoning: str

guardrail_agent = Agent( # (1)!
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
)


@input_guardrail
async def math_guardrail( # (2)!
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, input, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output, # (3)!
        tripwire_triggered=result.final_output.is_math_homework,
    )


agent = Agent(  # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    input_guardrails=[math_guardrail],
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except InputGuardrailTripwireTriggered:
        print("Math homework guardrail tripped")
```

1. このエージェントをガードレール関数内で使用します。
2. これはエージェントの入力/コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果に追加情報を含めることができます。
4. これはワークフローを定義する実際のエージェントです。

出力ガードレールも同様です。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    output_guardrail,
)
class MessageOutput(BaseModel): # (1)!
    response: str

class MathOutput(BaseModel): # (2)!
    reasoning: str
    is_math: bool

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the output includes any math.",
    output_type=MathOutput,
)

@output_guardrail
async def math_guardrail(  # (3)!
    ctx: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, output.response, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=result.final_output.is_math,
    )

agent = Agent( # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    output_guardrails=[math_guardrail],
    output_type=MessageOutput,
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except OutputGuardrailTripwireTriggered:
        print("Math output guardrail tripped")
```

1. これは実際のエージェントの出力型です。
2. これはガードレールの出力型です。
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際のエージェントです。