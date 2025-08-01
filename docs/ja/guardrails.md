---
search:
  exclude: true
---
# ガードレール

ガードレールは _並行して_ エージェントと動作し、ユーザー入力のチェックとバリデーションを行えます。たとえば、非常に賢い（ゆえに遅く/高価な）モデルを用いて顧客対応を行うエージェントがあるとします。不正なユーザーがそのモデルに数学の宿題を解かせようとするのは避けたいでしょう。そこで、低コストで高速なモデルを使ったガードレールを実行できます。ガードレールが悪意ある利用を検出した場合、ただちにエラーを発生させて高価なモデルの実行を中止し、時間とコストを節約できます。

ガードレールには 2 種類あります：

1. 入力ガードレールは初回のユーザー入力に対して実行されます  
2. 出力ガードレールは最終的なエージェントの出力に対して実行されます  

## 入力ガードレール

入力ガードレールは次の 3 ステップで実行されます：

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。その結果は [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。  
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。 true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへの応答や例外処理を適切に行えます。  

!!! Note

    入力ガードレールはユーザー入力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最初* のエージェントである場合にのみ実行されます。「なぜ `guardrails` プロパティが `Runner.run` への引数ではなくエージェントにあるのか？」と疑問に思うかもしれません。ガードレールは実際の エージェント に密接に関連する傾向があり、エージェントごとに異なるガードレールを走らせることになるので、コードを同じ場所に置くほうが可読性に優れるためです。

## 出力ガードレール

出力ガードレールは次の 3 ステップで実行されます：

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。その結果は [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。  
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。 true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへの応答や例外処理を適切に行えます。  

!!! Note

    出力ガードレールは最終出力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最後* のエージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際の エージェント に関連しているため、エージェントごとに異なるガードレールを設定できるようコードを同じ場所に置いておくと可読性が向上します。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでこれを通知できます。トリップワイヤーが発動したガードレールを検出するとただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部で エージェント を実行して実現します。

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
2. これがエージェントの入力/コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレールの結果に追加情報を含めることもできます。  
4. こちらがワークフローを定義する実際のエージェントです。  

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

1. こちらが実際のエージェントの出力型です。  
2. こちらがガードレールの出力型です。  
3. これがエージェントの出力を受け取り、結果を返すガードレール関数です。  
4. こちらがワークフローを定義する実際のエージェントです。  