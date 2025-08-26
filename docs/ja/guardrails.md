---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _ 並行して _ 実行され、 ユーザー 入力のチェックや検証を行えます。たとえば、 とても賢い（つまり遅く/高コストな）モデルを使って カスタマーリクエスト を支援する エージェント があるとします。悪意のある ユーザー がそのモデルに数学の宿題を手伝わせるよう依頼するのは避けたいはずです。そこで、 高速/低コスト なモデルでガードレールを実行できます。ガードレールが不正な利用を検知した場合、すぐにエラーを発生させ、 高コスト なモデルの実行を停止して時間や費用を節約できます。

ガードレールには 2 種類あります。

1. 入力ガードレールは最初の ユーザー 入力で実行されます
2. 出力ガードレールは最終的な エージェント 出力で実行されます

## 入力ガードレール

入力ガードレールは次の 3 段階で実行されます。

1. まず、ガードレールは エージェント に渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、適切に ユーザー へ応答するか、例外を処理できます。

!!! Note

    入力ガードレールは ユーザー 入力での実行を想定しているため、 エージェント のガードレールはその エージェント が * 最初 * の エージェント の場合にのみ実行されます。なぜ `guardrails` プロパティが エージェント 上にあり、`Runner.run` へ渡さないのか疑問に思うかもしれません。これは、ガードレールが実際の エージェント と密接に関連する傾向があるためです。 エージェント ごとに異なるガードレールを実行するので、コードを同じ場所に置くことが可読性の観点から有用です。

## 出力ガードレール

出力ガードレールは次の 3 段階で実行されます。

1. まず、ガードレールは エージェント によって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、適切に ユーザー へ応答するか、例外を処理できます。

!!! Note

    出力ガードレールは最終的な エージェント 出力での実行を想定しているため、 エージェント のガードレールはその エージェント が * 最後 * の エージェント の場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際の エージェント と密接に関連する傾向があるため、コードを同じ場所に置くことが可読性の観点から有用です。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでそれを示せます。トリップワイヤーが作動したガードレールを検知した時点で、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、 エージェント の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、その内部で エージェント を実行して実現します。

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

1. この エージェント をガードレール関数内で使用します。
2. これは エージェント の入力/コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果に追加情報を含めることができます。
4. これはワークフローを定義する実際の エージェント です。

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

1. これは実際の エージェント の出力型です。
2. これはガードレールの出力型です。
3. これは エージェント の出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際の エージェント です。