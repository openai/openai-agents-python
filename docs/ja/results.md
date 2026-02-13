---
search:
  exclude: true
---
# 実行結果

`Runner.run` メソッドを呼び出すと、次のいずれかが返ります。

- `run` または `run_sync` を呼び出した場合は [`RunResult`][agents.result.RunResult]
- `run_streamed` を呼び出した場合は [`RunResultStreaming`][agents.result.RunResultStreaming]

これらはいずれも [`RunResultBase`][agents.result.RunResultBase] を継承しており、有用な情報の多くはそこに含まれています。

## 最終出力

[`final_output`][agents.result.RunResultBase.final_output] プロパティには、最後に実行されたエージェントの最終出力が含まれます。これは次のいずれかです。

- 最後のエージェントに `output_type` が定義されていない場合は `str`
- エージェントに出力型が定義されている場合は `last_agent.output_type` 型のオブジェクト

!!! note

    `final_output` は `Any` 型です。ハンドオフがあるため、静的に型付けできません。ハンドオフが発生すると、どの Agent も最後のエージェントになり得るため、可能な出力型の集合を静的には把握できません。

## 次のターンの入力

[`result.to_input_list()`][agents.result.RunResultBase.to_input_list] を使用すると、実行結果を入力リストに変換できます。この入力リストは、あなたが提供した元の入力に、エージェント実行中に生成された項目を連結したものです。これにより、あるエージェント実行の出力を別の実行に渡したり、ループで実行して毎回新しいユーザー入力を追記したりするのが便利になります。

## 最後のエージェント

[`last_agent`][agents.result.RunResultBase.last_agent] プロパティには、最後に実行されたエージェントが含まれます。アプリケーションによっては、ユーザーが次に何かを入力したときにこれが役立つことがよくあります。たとえば、フロントのトリアージ エージェントが言語別エージェントへハンドオフする場合、最後のエージェントを保存しておき、次にユーザーがエージェントへメッセージを送る際に再利用できます。

## 新規項目

[`new_items`][agents.result.RunResultBase.new_items] プロパティには、実行中に生成された新しい項目が含まれます。項目は [`RunItem`][agents.items.RunItem] です。実行項目は、LLM が生成した raw の項目をラップします。

- [`MessageOutputItem`][agents.items.MessageOutputItem] は、LLM からのメッセージを示します。raw の項目は生成されたメッセージです。
- [`HandoffCallItem`][agents.items.HandoffCallItem] は、LLM がハンドオフ ツールを呼び出したことを示します。raw の項目は、LLM からのツール呼び出し項目です。
- [`HandoffOutputItem`][agents.items.HandoffOutputItem] は、ハンドオフが発生したことを示します。raw の項目は、ハンドオフ ツール呼び出しに対するツール応答です。項目から source / target のエージェントにもアクセスできます。
- [`ToolCallItem`][agents.items.ToolCallItem] は、LLM がツールを呼び出したことを示します。
- [`ToolCallOutputItem`][agents.items.ToolCallOutputItem] は、ツールが呼び出されたことを示します。raw の項目はツール応答です。項目からツール出力にもアクセスできます。
- [`ReasoningItem`][agents.items.ReasoningItem] は、LLM からの推論項目を示します。raw の項目は生成された推論です。

## その他の情報

### ガードレールの実行結果

[`input_guardrail_results`][agents.result.RunResultBase.input_guardrail_results] および [`output_guardrail_results`][agents.result.RunResultBase.output_guardrail_results] プロパティには、存在する場合にガードレールの実行結果が含まれます。ガードレールの実行結果には、ログに記録したり保存したりしたい有用な情報が含まれることがあるため、これらを利用できるようにしています。

ツールのガードレール実行結果は、[`tool_input_guardrail_results`][agents.result.RunResultBase.tool_input_guardrail_results] および [`tool_output_guardrail_results`][agents.result.RunResultBase.tool_output_guardrail_results] として別途利用できます。これらのガードレールはツールに紐づけることができ、そのツール呼び出しではエージェントのワークフロー中にガードレールが実行されます。

### raw 応答

[`raw_responses`][agents.result.RunResultBase.raw_responses] プロパティには、LLM により生成された [`ModelResponse`][agents.items.ModelResponse] が含まれます。

### 元の入力

[`input`][agents.result.RunResultBase.input] プロパティには、`run` メソッドに提供した元の入力が含まれます。多くの場合は不要ですが、必要になった場合に備えて利用できます。

### 中断と実行の再開

ツール承認のために実行が一時停止した場合、保留中の承認は [`interruptions`][agents.result.RunResultBase.interruptions] に公開されます。`to_state()` で実行結果を [`RunState`][agents.run_state.RunState] に変換し、中断を承認または拒否してから、`Runner.run(...)` または `Runner.run_streamed(...)` で再開します。

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="Use tools when needed.")
result = await Runner.run(agent, "Delete temp files that are no longer needed.")

if result.interruptions:
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    result = await Runner.run(agent, state)
```

[`RunResult`][agents.result.RunResult] と [`RunResultStreaming`][agents.result.RunResultStreaming] は、どちらも `to_state()` をサポートします。

### 便利なヘルパー

`RunResultBase` には、本番フローで有用なヘルパー メソッド / プロパティがいくつか含まれています。

- [`final_output_as(...)`][agents.result.RunResultBase.final_output_as] は、最終出力を特定の型にキャストします（任意で実行時の型チェックも行います）。
- [`last_response_id`][agents.result.RunResultBase.last_response_id] は、最新のモデル応答 ID を返し、応答チェーンに役立ちます。
- [`release_agents(...)`][agents.result.RunResultBase.release_agents] は、実行結果を確認した後にメモリ負荷を下げたい場合に、エージェントへの強参照を破棄します。