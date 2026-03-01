---
search:
  exclude: true
---
# ガードレール

ガードレールを使うと、ユーザー入力とエージェント出力のチェックと検証を行えます。たとえば、顧客リクエスト対応のために非常に賢い（したがって低速で高コストな）モデルを使うエージェントがあるとします。悪意のあるユーザーに、数学の宿題を手伝うようモデルへ依頼されるのは避けたいはずです。そのため、高速で低コストなモデルでガードレールを実行できます。ガードレールが悪意ある利用を検知した場合、即座にエラーを発生させて高コストなモデルの実行を防げるため、時間とコストを節約できます（ **ブロッキングガードレールを使用する場合。並列ガードレールでは、ガードレールが完了する前に高コストなモデルがすでに実行を開始している可能性があります。詳細は以下の「実行モード」を参照してください** ）。

ガードレールには 2 種類あります。

1. 入力ガードレールは初期ユーザー入力に対して実行されます
2. 出力ガードレールは最終エージェント出力に対して実行されます

## 入力ガードレール

入力ガードレールは 3 つのステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。これは [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへの適切な応答や例外処理を行えます。

!!! Note

    入力ガードレールはユーザー入力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最初* のエージェントである場合にのみ実行されます。`guardrails` プロパティを `Runner.run` に渡すのではなくエージェント側にある理由はなぜかと思うかもしれません。これは、ガードレールが実際の Agent に関連する傾向があるためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が高まります。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールはエージェント実行と同時に実行されます。両方が同時に開始されるため、最良のレイテンシーを得られます。ただし、ガードレールが失敗した場合、キャンセルされる前にエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールはエージェント開始 *前* に実行・完了します。ガードレールのトリップワイヤーが発火した場合、エージェントは実行されないため、トークン消費とツール実行を防げます。これはコスト最適化に最適であり、ツール呼び出しによる潜在的な副作用を避けたい場合に有効です。

## 出力ガードレール

出力ガードレールは 3 つのステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。これは [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへの適切な応答や例外処理を行えます。

!!! Note

    出力ガードレールは最終エージェント出力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最後* のエージェントである場合にのみ実行されます。入力ガードレールと同様に、これはガードレールが実際の Agent に関連する傾向があるためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が高まります。

    出力ガードレールは常にエージェント完了後に実行されるため、`run_in_parallel` パラメーターはサポートしていません。

## ツールガードレール

ツールガードレールは **関数ツール** をラップし、実行前後にツール呼び出しを検証またはブロックできます。設定はツール自体に対して行い、そのツールが呼び出されるたびに実行されます。

- 入力ツールガードレールはツール実行前に動作し、呼び出しのスキップ、出力のメッセージ置換、またはトリップワイヤーの発火ができます。
- 出力ツールガードレールはツール実行後に動作し、出力の置換またはトリップワイヤーの発火ができます。
- ツールガードレールは [`function_tool`][agents.function_tool] で作成された関数ツールにのみ適用されます。ホスト型ツール（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）および組み込み実行ツール（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）ではこのガードレールパイプラインは使用されません。

詳細は以下のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、Guardrail はトリップワイヤーでこれを通知できます。トリップワイヤーが発火したガードレールを検知すると、即座に `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、Agent の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を提供する必要があります。この例では、内部で Agent を実行することで実現します。

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
2. これは、エージェントの入力/コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果に追加情報を含められます。
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
3. これは、エージェントの出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際のエージェントです。

最後に、ツールガードレールの例を示します。

```python
import json
from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)

@tool_input_guardrail
def block_secrets(data):
    args = json.loads(data.context.tool_arguments or "{}")
    if "sk-" in json.dumps(args):
        return ToolGuardrailFunctionOutput.reject_content(
            "Remove secrets before calling this tool."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_output_guardrail
def redact_output(data):
    text = str(data.output or "")
    if "sk-" in text:
        return ToolGuardrailFunctionOutput.reject_content("Output contained sensitive data.")
    return ToolGuardrailFunctionOutput.allow()


@function_tool(
    tool_input_guardrails=[block_secrets],
    tool_output_guardrails=[redact_output],
)
def classify_text(text: str) -> str:
    """Classify text for internal routing."""
    return f"length:{len(text)}"


agent = Agent(name="Classifier", tools=[classify_text])
result = Runner.run_sync(agent, "hello world")
print(result.final_output)
```