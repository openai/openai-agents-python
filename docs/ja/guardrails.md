---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列_ に実行され、ユーザー入力のチェックとバリデーションを行えます。例えば、非常に賢い（その分、遅くて高価な）モデルを用いて顧客リクエストに対応するエージェントがあるとします。悪意のあるユーザーがそのモデルに数学の宿題を手伝わせるようなことは避けたいでしょう。そこで、高速かつ低コストのモデルでガードレールを走らせることができます。ガードレールが不正利用を検知したら、即座にエラーを発生させ、高価なモデルの実行を停止して時間とコストを節約します。

ガードレールには 2 種類あります。

1. 入力ガードレール: 初回のユーザー入力に対して実行されます  
2. 出力ガードレール: 最終的なエージェント出力に対して実行されます  

## 入力ガードレール

入力ガードレールは次の 3 ステップで動作します。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるので、適切にユーザーへ応答したり例外処理を行ったりできます。  

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントが *最初* のエージェントである場合にのみ実行されます。「`guardrails` プロパティがエージェントにあるのはなぜで、`Runner.run` に渡さないのか」と疑問に思うかもしれません。ガードレールは実際のエージェントに密接に関連することが多く、エージェントごとに異なるガードレールを走らせるため、コードを同じ場所に置いておくほうが可読性に優れるからです。

## 出力ガードレール

出力ガードレールは次の 3 ステップで動作します。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるので、適切にユーザーへ応答したり例外処理を行ったりできます。  

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントが *最後* のエージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際のエージェントに密接に関連するので、コードを同じ場所に置いておくほうが可読性に優れます。

## トリップワイヤ

入力または出力がガードレールを通過できなかった場合、ガードレールはトリップワイヤを発動してその事実を通知できます。トリップワイヤが発動したガードレールを検知した時点で、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。以下の例では、その関数内でエージェントを実行しています。

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
2. これがガードレール関数で、エージェントの入力／コンテキストを受け取り結果を返します。  
3. ガードレールの結果に追加情報を含めることもできます。  
4. これが実際にワークフローを定義するエージェントです。  

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
3. これがエージェントの出力を受け取り結果を返すガードレール関数です。  
4. これが実際にワークフローを定義するエージェントです。