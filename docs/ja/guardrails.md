# ガードレール

ガードレールはエージェントと _並行して_ 実行され、ユーザー入力のチェックと検証を可能にします。例えば、非常に賢い（したがって遅く/高価な）モデルを使用して顧客のリクエストを支援するエージェントがあるとします。悪意のあるユーザーがモデルに数学の宿題を手伝わせるようなことは避けたいでしょう。そこで、速く/安価なモデルでガードレールを実行できます。ガードレールが悪意のある使用を検出した場合、即座にエラーを発生させ、高価なモデルの実行を停止し、時間とお金を節約できます。

ガードレールには2種類あります：

1. 入力ガードレールは初期のユーザー入力に対して実行されます
2. 出力ガードレールは最終的なエージェントの出力に対して実行されます

## 入力ガードレール

入力ガードレールは3つのステップで実行されます：

1. まず、ガードレールはエージェントに渡されたのと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]を生成し、それが[`InputGuardrailResult`][agents.guardrail.InputGuardrailResult]でラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered]例外が発生し、ユーザーに適切に応答したり、例外を処理したりできます。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを意図しているため、エージェントのガードレールはエージェントが*最初の*エージェントである場合にのみ実行されます。なぜ `guardrails` プロパティがエージェントにあり、`Runner.run` に渡されないのか疑問に思うかもしれません。それは、ガードレールが実際のエージェントに関連している傾向があるためです。異なるエージェントには異なるガードレールを実行するので、コードを一緒に配置することは可読性に役立ちます。

## 出力ガードレール

出力ガードレールは3つのステップで実行されます：

1. まず、ガードレールはエージェントに渡されたのと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]を生成し、それが[`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult]でラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered]が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered]例外が発生し、ユーザーに適切に応答したり、例外を処理したりできます。

!!! Note

    出力ガードレールは最終的なエージェントの出力に対して実行されることを意図しているため、エージェントのガードレールはエージェントが*最後の*エージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールが実際のエージェントに関連している傾向があるため、コードを一緒に配置することは可読性に役立ちます。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでこれを知らせることができます。トリップワイヤーがトリガーされたガードレールを確認するとすぐに、`{Input,Output}GuardrailTripwireTriggered`例外を即座に発生させ、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput]を返す関数を提供する必要があります。この例では、エージェントを内部で実行することでこれを行います。

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

1. このエージェントをガードレール関数で使用します。
2. これはエージェントの入力/コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレールの結果に追加情報を含めることができます。
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

1. これは実際のエージェントの出力タイプです。
2. これはガードレールの出力タイプです。
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際のエージェントです。