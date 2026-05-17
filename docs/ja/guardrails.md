---
search:
  exclude: true
---
# ガードレール

ガードレールを使用すると、ユーザー入力とエージェント出力のチェックと検証を行えます。例えば、顧客リクエストの支援に非常に賢い（そのため遅く高コストな）モデルを使用するエージェントがあるとします。悪意あるユーザーが、そのモデルに数学の宿題を手伝わせることは望ましくありません。そのため、高速/低コストなモデルでガードレールを実行できます。ガードレールが悪用を検出した場合、ただちにエラーを発生させ、高コストなモデルの実行を防ぐことができ、時間とコストを節約できます（ **ブロッキングガードレールを使用している場合です。並列ガードレールでは、ガードレールが完了する前に高コストなモデルの実行がすでに開始している可能性があります。詳細は以下の「実行モード」を参照してください** ）。

ガードレールには 2 種類あります。

1. 入力ガードレールは最初のユーザー入力に対して実行されます
2. 出力ガードレールは最終的なエージェント出力に対して実行されます

## ワークフローの境界

ガードレールはエージェントやツールに付与されますが、ワークフロー内の同じ時点ですべてが実行されるわけではありません。

-   **入力ガードレール** は、チェーン内の最初のエージェントに対してのみ実行されます。
-   **出力ガードレール** は、最終出力を生成するエージェントに対してのみ実行されます。
-   **ツールガードレール** は、すべてのカスタム関数ツール呼び出しで実行されます。入力ガードレールは実行前に、出力ガードレールは実行後に実行されます。

マネージャー、ハンドオフ、または委任先のスペシャリストを含むワークフローで、各カスタム関数ツール呼び出しの周囲にチェックが必要な場合は、エージェントレベルの入力/出力ガードレールだけに頼るのではなく、ツールガードレールを使用してください。

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true であるかどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへ適切に応答したり、例外を処理したりできます。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを意図しているため、エージェントのガードレールは、そのエージェントが *最初の* エージェントである場合にのみ実行されます。なぜ `guardrails` プロパティが `Runner.run` に渡されるのではなく、エージェント上にあるのか疑問に思うかもしれません。これは、ガードレールが実際のエージェントに関連することが多いためです。エージェントごとに異なるガードレールを実行することになるため、コードを同じ場所に配置すると読みやすさの面で有用です。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行** （デフォルト、 `run_in_parallel=True` ）: ガードレールはエージェントの実行と同時に実行されます。両方が同時に開始するため、レイテンシーの面で最良です。ただし、ガードレールが失敗した場合、キャンセルされる前にエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行** （ `run_in_parallel=False` ）: ガードレールはエージェントの開始 *前に* 実行され、完了します。ガードレールのトリップワイヤーがトリガーされた場合、エージェントは一切実行されないため、トークン消費とツール実行を防げます。これは、コスト最適化や、ツール呼び出しによる潜在的な副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントによって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true であるかどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへ適切に応答したり、例外を処理したりできます。

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを意図しているため、エージェントのガードレールは、そのエージェントが *最後の* エージェントである場合にのみ実行されます。入力ガードレールと同様に、これはガードレールが実際のエージェントに関連することが多いためです。エージェントごとに異なるガードレールを実行することになるため、コードを同じ場所に配置すると読みやすさの面で有用です。

    出力ガードレールは常にエージェントの完了後に実行されるため、 `run_in_parallel` パラメーターはサポートしていません。

## ツールガードレール

ツールガードレールは **関数ツール** をラップし、実行前後にツール呼び出しを検証またはブロックできるようにします。ツール自体に設定され、そのツールが呼び出されるたびに実行されます。

- 入力ツールガードレールはツールの実行前に実行され、呼び出しのスキップ、出力のメッセージへの置き換え、またはトリップワイヤーの発生を行えます。
- 出力ツールガードレールはツールの実行後に実行され、出力の置き換え、またはトリップワイヤーの発生を行えます。
- ツールガードレールは、[`function_tool`][agents.tool.function_tool] で作成された関数ツールにのみ適用されます。ハンドオフは通常の関数ツールパイプラインではなく SDK のハンドオフパイプラインを通るため、ツールガードレールはハンドオフ呼び出し自体には適用されません。ホスト型ツール（ `WebSearchTool` 、 `FileSearchTool` 、 `HostedMCPTool` 、 `CodeInterpreterTool` 、 `ImageGenerationTool` ）や組み込み実行ツール（ `ComputerTool` 、 `ShellTool` 、 `ApplyPatchTool` 、 `LocalShellTool` ）もこのガードレールパイプラインを使用しません。また、[`Agent.as_tool()`][agents.agent.Agent.as_tool] は現在、ツールガードレールのオプションを直接公開していません。

詳細は以下のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールの検査に合格しない場合、ガードレールはトリップワイヤーでこれを通知できます。トリップワイヤーがトリガーされたガードレールを検出すると、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、エージェント実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部でエージェントを実行することでこれを行います。

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
2. これは、エージェントの入力/コンテキストを受け取り、実行結果を返すガードレール関数です。
3. ガードレールの実行結果に追加情報を含めることができます。
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
3. これは、エージェントの出力を受け取り、実行結果を返すガードレール関数です。
4. これはワークフローを定義する実際のエージェントです。

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