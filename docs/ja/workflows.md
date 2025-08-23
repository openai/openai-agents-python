---
search:
  exclude: true
---
# ワークフロー

!!! warning "ベータ機能"
    
    ワークフローシステムは現在ベータ版です。APIは将来のリリースで変更される可能性があります。この機能の安定化に向けて、フィードバックや貢献をお待ちしています。

ワークフローは、複雑なマルチエージェントの相互作用を宣言的に調整する方法を提供します。コード内でエージェントの呼び出しを手動でチェーンする代わりに、エージェント間の接続のシーケンスとしてフローを定義できるため、マルチエージェントシステムがより読みやすく、保守しやすく、再利用可能になります。

ワークフローは、エージェント同士がどのように相互作用するかを定義する **接続** のシーケンスで構成されます。各接続タイプは、シンプルなハンドオフから並列処理まで、異なる相互作用パターンを実装します。

## 条件付き実行

ワークフローは **条件付き実行** をサポートするようになりました - 前提条件が満たされない場合、接続がスキップされる可能性があります。例えば：

- **SequentialConnection** と **ToolConnection** は、それらの `from_agent` が現在アクティブな場合のみ実行されます
- **HandoffConnection** が実際のハンドオフにつながらない場合、ターゲットエージェントに依存する後続の接続はスキップされます
- これにより、ランタイムの決定に基づいたより知的なワークフロールーティングが可能になります

ワークフロー実行エンジンは、どの接続がスキップされたかを追跡し、`WorkflowResult.skipped_connections` フィールドで報告します。

## 基本的な使い方

3つのエージェントをチェーンする簡単なワークフローの例：

```python
from agents import Agent, Workflow, HandoffConnection, ToolConnection

# エージェントを定義
triage_agent = Agent(name="Triage Agent", instructions="リクエストを専門家にルーティング")
specialist_agent = Agent(name="Specialist", instructions="専門的なリクエストを処理") 
summary_agent = Agent(name="Summary Agent", instructions="最終的な要約を作成")

# ワークフローを作成
workflow = Workflow([
    HandoffConnection(triage_agent, specialist_agent),
    ToolConnection(specialist_agent, summary_agent),
])

# ワークフローを実行
result = await workflow.run("リクエストを手伝ってください")
print(result.final_result.final_output)
```

## 接続タイプ

ワークフローシステムは、それぞれ異なる相互作用パターンを実装するいくつかの接続タイプをサポートしています：

### HandoffConnection

あるエージェントから別のエージェントに制御を移譲します。ターゲットエージェントが会話を引き継ぎ、完全な会話履歴を見ることができます。

```python
from agents import HandoffConnection

connection = HandoffConnection(
    from_agent=triage_agent,
    to_agent=billing_agent,
    tool_description_override="請求専門家に移譲",
    input_filter=custom_filter_function,  # オプション
)
```

**使用例**: ルーティング、委譲、専門化

### ToolConnection

ターゲットエージェントをソースエージェントのツールとして使用します。ソースエージェントがターゲットエージェントを関数のように呼び出し、結果を受けて続行します。

```python
from agents import ToolConnection

connection = ToolConnection(
    from_agent=main_agent,
    to_agent=analysis_agent,
    tool_name="analyze_data",
    tool_description="詳細な分析を取得",
    custom_output_extractor=lambda result: result.final_output.summary,
)
```

**使用例**: モジュラー機能、分析、データ処理

### SequentialConnection

あるエージェントの出力を別のエージェントの入力として渡し、データ変換パイプラインを作成します。

```python
from agents import SequentialConnection

connection = SequentialConnection(
    from_agent=research_agent,
    to_agent=writer_agent,
    output_transformer=lambda result: f"研究結果: {result.final_output}",
)
```

**使用例**: データパイプライン、多段階変換、処理チェーン

### ConditionalConnection

条件に基づいて異なるエージェントにルーティングし、動的なワークフロー分岐を可能にします。

```python
from agents import ConditionalConnection

def should_escalate(context, previous_result):
    return context.context.priority == "high"

connection = ConditionalConnection(
    from_agent=support_agent,
    to_agent=standard_agent,
    alternative_agent=escalation_agent,
    condition=should_escalate,
)
```

**使用例**: 動的ルーティング、条件ロジック、適応型ワークフロー

### ParallelConnection

複数のエージェントを同時に実行し、オプションでそれらの結果を統合します。

```python
from agents import ParallelConnection

connection = ParallelConnection(
    from_agent=coordinator_agent,
    to_agent=coordinator_agent,  # 並列実行では使用されない
    parallel_agents=[technical_agent, business_agent, legal_agent],
    synthesizer_agent=synthesis_agent,
    synthesis_template="これらの視点を組み合わせる: {results}",
)
```

**使用例**: 並行処理、複数の視点、パフォーマンス最適化

## ワークフロー設定

### 基本設定

```python
from agents import Workflow

workflow = Workflow(
    connections=[connection1, connection2, connection3],
    name="My Workflow",                    # オプション: トレーシング用
    context=my_context,                    # オプション: 共有コンテキスト
    max_steps=50,                          # オプション: 安全制限
    trace_workflow=True,                   # オプション: トレーシング有効化
)
```

### コンテキスト管理

ワークフローは全エージェント間での共有コンテキストをサポートします：

```python
from pydantic import BaseModel

class WorkflowContext(BaseModel):
    user_id: str
    session_data: dict = {}
    
context = WorkflowContext(user_id="123")
workflow = Workflow(connections=[...], context=context)

result = await workflow.run("こんにちは")
# コンテキストは共有され、エージェントによって変更可能
print(result.context.session_data)
```

## 実行

### 非同期実行

```python
result = await workflow.run("ここに入力")

# 結果にアクセス
print(result.final_result.final_output)  # 最終出力
print(len(result.step_results))          # 実行されたステップ数
print(result.context)                    # 最終コンテキスト状態
```

### 同期実行

```python
# より簡単なスクリプトやテスト用
result = workflow.run_sync("ここに入力")
print(result.final_result.final_output)
```

## ベストプラクティス

### 1. 明確性を重視した設計

ワークフローを自己文書化するように作成：

```python
workflow = Workflow([
    HandoffConnection(
        from_agent=intake_agent,
        to_agent=specialist_agent,
        tool_description_override="適切な専門家にルーティング",
    ),
    ToolConnection(
        from_agent=specialist_agent,
        to_agent=analysis_agent,
        tool_name="analyze_request",
        tool_description="詳細分析を実行",
    ),
], name="顧客リクエスト処理")
```

### 2. 適切な接続タイプの使用

使用例に適した接続タイプを選択：

- **HandoffConnection**: 完全な会話移譲が必要な場合
- **ToolConnection**: モジュラーで再利用可能な機能が必要な場合
- **SequentialConnection**: データ変換パイプラインが必要な場合
- **ConditionalConnection**: 動的ルーティングが必要な場合
- **ParallelConnection**: 並行処理が必要な場合

## 例

完全な例については [`examples/workflows`](https://github.com/openai/openai-agents-python/tree/main/examples/workflows) ディレクトリを参照してください：

- **[basic_workflow.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/basic_workflow.py)**: 全接続タイプを使用したシンプルなワークフロー
- **[advanced_workflow.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/advanced_workflow.py)**: 複雑なオーケストレーションパターン
- **[comprehensive_example.py](https://github.com/openai/openai-agents-python/tree/main/examples/workflows/comprehensive_example.py)**: フル機能のワークフローシステム
