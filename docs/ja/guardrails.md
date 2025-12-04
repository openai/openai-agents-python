---
search:
  exclude: true
---
# ガードレール

ガードレールは、 ユーザー 入力および エージェント 出力のチェックと検証を可能にします。たとえば、非常に賢い（つまり遅く/高価な）モデルを使って顧客からのリクエストを支援する エージェント を想像してください。悪意のある ユーザー が、そのモデルに数学の宿題を手伝わせるよう求めるのは避けたいはずです。そこで、速く/安価なモデルでガードレールを実行できます。ガードレールが悪意のある使用を検知した場合、即座にエラーを発生させて高価なモデルの実行を防ぎ、時間とコストを節約できます（ **ブロッキング型のガードレールを使用する場合。並列ガードレールでは、ガードレールの完了前に高価なモデルがすでに実行を開始している可能性があります。詳細は下記の「実行モード」を参照してください** ）。

ガードレールには 2 つの種類があります。

1. 入力ガードレールは最初の ユーザー 入力に対して実行されます
2. 出力ガードレールは最終的な エージェント 出力に対して実行されます

## 入力ガードレール

入力ガードレールは 3 つの手順で実行されます。

1. まず、ガードレールは エージェント に渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、 ユーザー への適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレールは ユーザー 入力で実行されることを意図しているため、 エージェント のガードレールが実行されるのは、その エージェント が「最初」の エージェント の場合のみです。「guardrails」プロパティが エージェント 側にあり、`Runner.run` に渡さないのはなぜかと疑問に思うかもしれません。これは、ガードレールが実際の エージェント に密接に関連する傾向があるためです。エージェント ごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上します。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールは エージェント の実行と同時に走ります。両者が同時に開始されるため、レイテンシ面で最良です。ただし、ガードレールが失敗した場合、キャンセルされるまでに エージェント がすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールは エージェント の開始「前」に実行・完了します。ガードレールのトリップワイヤーが発火した場合、 エージェント は実行されず、トークン消費やツール実行を防げます。コスト最適化や、ツール呼び出しによる副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 つの手順で実行されます。

1. まず、ガードレールは エージェント によって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、 ユーザー への適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレールは最終的な エージェント 出力で実行されることを意図しているため、 エージェント のガードレールが実行されるのは、その エージェント が「最後」の エージェント の場合のみです。入力ガードレールと同様に、ガードレールは実際の エージェント に関連する傾向があるため、コードを同じ場所に置くことが可読性のために有用です。

    出力ガードレールは常に エージェント の完了後に実行されるため、`run_in_parallel` パラメーターはサポートしません。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーでそれを示すことができます。トリップワイヤーが発火したガードレールを検知すると直ちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、 エージェント の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部で エージェント を実行してこれを行います。

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
3. ガードレールの結果に追加情報を含めることができます。
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