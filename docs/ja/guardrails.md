---
search:
  exclude: true
---
# ガードレール

ガードレールは、 ユーザー 入力やエージェント出力に対するチェックとバリデーションを可能にします。例えば、顧客からのリクエスト対応に非常に賢い（その分、遅く/高価な）モデルを使うエージェントがあるとします。悪意のある ユーザー がモデルに数学の宿題を手伝わせるよう求めることは避けたいはずです。そのため、速く/安価なモデルでガードレールを実行できます。ガードレールが悪意ある利用を検知した場合、ただちにエラーを送出し、高価なモデルの実行を防げるため、時間と費用を節約できます（ **ブロッキング ガードレールを使用する場合**。並列ガードレールでは、ガードレールの完了前に高価なモデルが実行を開始している可能性があります。詳細は下記「実行モード」を参照してください）。

ガードレールには 2 種類あります:

1. 入力ガードレールは最初の ユーザー 入力で実行されます
2. 出力ガードレールは最終的なエージェント出力で実行されます

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます:

1. 最初に、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップします。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外を送出し、 ユーザー への適切な対応や例外処理ができるようにします。

!!! Note

    入力ガードレールは ユーザー 入力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが「最初の」エージェントの場合にのみ実行されます。なぜ `guardrails` プロパティがエージェント側にあり、`Runner.run` に渡さないのか疑問に思うかもしれません。これは、ガードレールが実際のエージェントに密接に関連する傾向があるためです。エージェントごとに異なるガードレールを実行することになるため、コードを同じ場所に置くことは可読性の面で有用です。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします:

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールはエージェントの実行と同時に並行して動作します。両者が同時に開始するため、待ち時間に最も優れます。ただし、ガードレールが失敗した場合、キャンセルされるまでにエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールはエージェントが開始する「前に」実行・完了します。ガードレールのトリップワイヤーが発火した場合、エージェントは実行されず、トークン消費やツール実行を防げます。これはコスト最適化や、ツール呼び出しによる副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます:

1. 最初に、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップします。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外を送出し、 ユーザー への適切な対応や例外処理ができるようにします。

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが「最後の」エージェントの場合にのみ実行されます。入力ガードレールと同様、ガードレールは実際のエージェントに密接に関連する傾向があるため、コードを同じ場所に置くことは可読性の面で有用です。

    出力ガードレールは常にエージェントの完了後に実行されるため、`run_in_parallel` パラメーターはサポートしません。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーでこれを通知できます。トリップワイヤーが発火したガードレールを検知するとすぐに、`{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。以下の例では、その裏でエージェントを実行することで実現します。

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
3. ガードレール結果に追加情報を含めることができます。
4. これがワークフローを定義する実際のエージェントです。

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
3. これがエージェントの出力を受け取り、結果を返すガードレール関数です。
4. これがワークフローを定義する実際のエージェントです。