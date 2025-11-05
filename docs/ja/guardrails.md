---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並行して_ 実行され、ユーザー入力のチェックや検証を行います。たとえば、非常に賢い（つまり遅く/高価な）モデルで顧客からのリクエストを手伝うエージェントがあるとします。悪意のあるユーザーが数学の宿題を手伝わせるような要求をするのは避けたいはずです。そのため、ガードレールを高速/低コストのモデルで実行できます。ガードレールが悪用を検知した場合は即座にエラーを発生させ、コストの高いモデルの実行を止めて時間と費用を節約できます。

ガードレールには 2 種類あります。

1. 入力ガードレール（初回のユーザー入力に対して実行）
2. 出力ガードレール（最終的なエージェント出力に対して実行）

## 入力ガードレール

入力ガードレールは 3 ステップで動作します。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数を実行して [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップします。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントのガードレールが実行されるのは、そのエージェントが「最初の」エージェントの場合のみです。「guardrails」プロパティをエージェントに持たせ、`Runner.run` に渡さないのはなぜか、と思うかもしれません。ガードレールは実際のエージェントに密接に関係する傾向があり、エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置く方が可読性の面で有用だからです。

## 出力ガードレール

出力ガードレールは 3 ステップで動作します。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数を実行して [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップします。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントのガードレールが実行されるのは、そのエージェントが「最後の」エージェントの場合のみです。入力ガードレールと同様に、ガードレールは実際のエージェントに密接に関係する傾向があるため、コードを同じ場所に置く方が可読性の面で有用です。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーによってそれを通知できます。トリップワイヤーが作動したガードレールを検知したらすぐに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部でエージェントを実行して実現します。

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