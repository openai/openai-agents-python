---
search:
  exclude: true
---
# ガードレール

ガードレール は _並行して_ エージェント を実行し、 ユーザー の入力に対するチェックや検証を可能にします。たとえば、顧客からのリクエスト対応に非常に高性能（そのため遅く/高価）なモデルを使う エージェント があるとします。悪意のある ユーザー がそのモデルに数学の宿題の手伝いをさせるのは避けたいはずです。そこで、速く/安価なモデルで ガードレール を走らせます。ガードレール が不正な利用を検知した場合は、即座にエラーを送出して高価なモデルの実行を停止し、時間やコストを節約できます。

ガードレール には 2 種類あります:

1. 入力ガードレール は初期の ユーザー 入力に対して実行されます
2. 出力ガードレール は最終的な エージェント の出力に対して実行されます

## 入力ガードレール

入力ガードレール は 3 ステップで動作します:

1. まず、エージェント に渡されたものと同じ入力を ガードレール が受け取ります。
2. 次に、ガードレール 関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、 ユーザー への適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレール は ユーザー 入力での実行を想定しているため、エージェント の ガードレール は、その エージェント が最初のエージェントである場合にのみ実行されます。なぜ `guardrails` プロパティが エージェント 側にあり、`Runner.run` に渡さないのかと疑問に思うかもしれません。これは、ガードレール は実際のエージェントに密接に関係する傾向があるためです。エージェント ごとに異なる ガードレール を実行することが多いため、コードを同じ場所に置くことで可読性が向上します。

## 出力ガードレール

出力ガードレール は 3 ステップで動作します:

1. まず、エージェント が生成した出力を ガードレール が受け取ります。
2. 次に、ガードレール 関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、 ユーザー への適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレール は最終的な エージェント の出力での実行を想定しているため、エージェント の ガードレール は、その エージェント が最後のエージェントである場合にのみ実行されます。入力ガードレール と同様に、ガードレール は実際のエージェントに密接に関係する傾向があるため、コードを同じ場所に置くことで可読性が向上します。

## トリップワイヤ

入力または出力が ガードレール に通らなかった場合、ガードレール はトリップワイヤでそれを通知できます。トリップワイヤが発動した ガードレール を検知するとすぐに、`{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェント の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、内部で エージェント を実行することでこれを行います。

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

1. この エージェント を ガードレール 関数内で使用します。
2. これは エージェント の入力/コンテキストを受け取り、結果を返す ガードレール 関数です。
3. ガードレール の結果に追加情報を含めることができます。
4. これはワークフローを定義する実際の エージェント です。

出力ガードレール も同様です。

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

1. これは実際の エージェント の出力型です。
2. これは ガードレール の出力型です。
3. これは エージェント の出力を受け取り、結果を返す ガードレール 関数です。
4. これはワークフローを定義する実際の エージェント です。