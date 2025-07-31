---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列_ で実行され、ユーザー入力のチェックとバリデーションを行えます。たとえば、顧客リクエストを処理するために非常に賢い（そのぶん遅く／高価な）モデルを使うエージェントがあるとします。悪意のあるユーザーに数学の宿題を手伝わせるような依頼をさせたくはありません。そのため、高速かつ低コストのモデルでガードレールを動かします。ガードレールが悪意のある利用を検出すると、即座にエラーを送出して高価なモデルの実行を停止し、時間とコストを節約できます。

ガードレールには 2 種類あります。

1. Input ガードレール：最初のユーザー入力に対して実行されます  
2. Output ガードレール：最終的なエージェント出力に対して実行されます  

## 入力ガードレール

入力ガードレールは次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップします。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合は [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外を送出し、ユーザーへの応答や例外処理を行います。  

!!! Note

    入力ガードレールはユーザー入力に対して動作することを想定しているため、エージェントが *最初* のエージェントである場合にのみ実行されます。`guardrails` プロパティがエージェントにあり、`Runner.run` に渡さない理由をご存じでしょうか。それは、ガードレールが実際のエージェントに強く関連するためです。エージェントごとに異なるガードレールを走らせることが多いため、コードを同じ場所に置くほうが読みやすくなるからです。

## 出力ガードレール

出力ガードレールは次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップします。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合は [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外を送出し、ユーザーへの応答や例外処理を行います。  

!!! Note

    出力ガードレールは最終的なエージェント出力に対して動作することを想定しているため、エージェントが *最後* のエージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際のエージェントに強く関連しているため、コードを同じ場所に置くほうが読みやすくなります。

## トリップワイヤー

入力または出力がガードレールを通過できなかった場合、ガードレールはトリップワイヤーでその事実を示します。トリップワイヤーを発動したガードレールが検出され次第、`{Input,Output}GuardrailTripwireTriggered` 例外を直ちに送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部でエージェントを実行してこれを行います。

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

1. ガードレール関数内で使用するエージェントです。  
2. これはエージェントの入力／コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレール結果に追加情報を含めることもできます。  
4. 実際のワークフローを定義するエージェントです。  

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
4. 実際のワークフローを定義するエージェントです。