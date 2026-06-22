---
search:
  exclude: true
---
# ガードレール

ガードレールを使用すると、ユーザー入力とエージェント出力のチェックおよび検証を行えます。たとえば、顧客からのリクエストを支援するために非常に賢い（そのため低速で高コストな）モデルを使用するエージェントがあるとします。悪意のあるユーザーに、そのモデルへ数学の宿題を手伝わせたくはないはずです。そこで、高速 / 低コストなモデルでガードレールを実行できます。ガードレールが悪意のある使用を検出した場合、即座にエラーを送出して高価なモデルの実行を防ぎ、時間と費用を節約できます（ **ブロッキングガードレールを使用する場合に限ります。並列ガードレールでは、ガードレールが完了する前に、高価なモデルがすでに実行を開始している可能性があります。詳細は下記の「実行モード」を参照してください** ）。

ガードレールには 2 種類あります。

1. 入力ガードレールは、最初のユーザー入力に対して実行されます。
2. 出力ガードレールは、最終的なエージェント出力に対して実行されます。

## ワークフローの境界

ガードレールはエージェントとツールにアタッチされますが、すべてがワークフロー内の同じ時点で実行されるわけではありません。

-   **入力ガードレール** は、チェーン内の最初のエージェントに対してのみ実行されます。
-   **出力ガードレール** は、最終出力を生成するエージェントに対してのみ実行されます。
-   **ツールガードレール** は、すべてのカスタム関数ツール呼び出しで実行され、実行前に入力ガードレール、実行後に出力ガードレールが実行されます。

マネージャー、ハンドオフ、または委任先の専門家を含むワークフローで、各カスタム関数ツール呼び出しの前後にチェックが必要な場合は、エージェントレベルの入力 / 出力ガードレールだけに頼るのではなく、ツールガードレールを使用してください。

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] が生成されます。これはその後 [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、 [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーに適切に応答したり、例外を処理したりできます。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが *最初の* エージェントである場合にのみ実行されます。なぜ `guardrails` プロパティが `Runner.run` に渡されるのではなく、エージェント上にあるのか疑問に思うかもしれません。これは、ガードレールが実際のエージェントに関連していることが多いためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に配置しておくと可読性の面で役立ちます。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行** （デフォルト、 `run_in_parallel=True` ）: ガードレールはエージェントの実行と並行して実行されます。両方が同時に開始されるため、レイテンシが最も良くなります。ただし、ガードレールが失敗した場合、キャンセルされる前にエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行** （ `run_in_parallel=False` ）: ガードレールはエージェントが開始する *前に* 実行され、完了します。ガードレールのトリップワイヤーが発火した場合、エージェントは一切実行されないため、トークン消費とツール実行を防げます。これは、コスト最適化や、ツール呼び出しによる潜在的な副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントによって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] が生成されます。これはその後 [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、 [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、 [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーに適切に応答したり、例外を処理したりできます。

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが *最後の* エージェントである場合にのみ実行されます。入力ガードレールと同様に、これはガードレールが実際のエージェントに関連していることが多いためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に配置しておくと可読性の面で役立ちます。

    出力ガードレールは必ずエージェントの完了後に実行されるため、 `run_in_parallel` パラメーターはサポートしません。

## ツールガードレール

ツールガードレールは **関数ツール** をラップし、実行の前後でツール呼び出しを検証またはブロックできるようにします。これはツール自体に設定され、そのツールが呼び出されるたびに実行されます。

- 入力ツールガードレールはツール実行前に実行され、呼び出しをスキップしたり、出力をメッセージに置き換えたり、トリップワイヤーを送出したりできます。
- 出力ツールガードレールはツール実行後に実行され、出力を置き換えたり、トリップワイヤーを送出したりできます。
- 関数ツールに承認が必要な場合、入力ツールガードレールは通常、承認後かつ実行直前に実行されます。保留中の承認割り込みが発行される前にこれらの入力チェックを実行したい場合は、 [`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] を [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig] に設定してください。この承認前チェックに合格した呼び出しも、ツールが実行される前に、承認後に再度チェックされます。
- ツールガードレールは、 [`function_tool`][agents.tool.function_tool] で作成された関数ツールにのみ適用されます。ハンドオフは通常の関数ツールパイプラインではなく、 SDK のハンドオフパイプラインを通じて実行されるため、ツールガードレールはハンドオフ呼び出し自体には適用されません。ホスト型ツール（ `WebSearchTool` 、 `FileSearchTool` 、 `HostedMCPTool` 、 `CodeInterpreterTool` 、 `ImageGenerationTool` ）と組み込み実行ツール（ `ComputerTool` 、 `ShellTool` 、 `ApplyPatchTool` 、 `LocalShellTool` ）もこのガードレールパイプラインを使用しません。また、 [`Agent.as_tool()`][agents.agent.Agent.as_tool] は現在、ツールガードレールのオプションを直接公開していません。

詳細は、下記のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールに合格しなかった場合、ガードレールはトリップワイヤーでこれを知らせることができます。トリップワイヤーが発火したガードレールを検出した時点で、即座に `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、 [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部でエージェントを実行することでこれを行います。

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
2. これは、エージェントの入力 / コンテキストを受け取り、実行結果を返すガードレール関数です。
3. ガードレールの実行結果に追加情報を含めることができます。
4. これは、ワークフローを定義する実際のエージェントです。

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

1. これは、実際のエージェントの出力型です。
2. これは、ガードレールの出力型です。
3. これは、エージェントの出力を受け取り、実行結果を返すガードレール関数です。
4. これは、ワークフローを定義する実際のエージェントです。

最後に、ツールガードレールのコード例を示します。

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