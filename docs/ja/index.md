---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応のアップグレードです。Agents SDK はごく少数の基本コンポーネントで構成されています:

-   **エージェント**、インストラクションとツールを備えた LLM
-   **ハンドオフ**、特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**、エージェントの入力と出力を検証できる機能
-   **セッション**、エージェントの実行間で会話履歴を自動的に保持する機能

これらの基本コンポーネントは、Python と組み合わせることでツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント フローの可視化・デバッグ・評価や、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

SDK の設計原則は 2 つあります:

1. 使う価値があるだけの機能を備えつつ、学習が速いよう基本コンポーネントは少数に抑える。
2. そのままでも優れた使い心地を提供しつつ、挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです:

-   エージェント ループ: ツールの呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを扱う組み込みのエージェント ループ。
-   Python ファースト: 新たな抽象を学ぶのではなく、言語の組み込み機能でエージェントをオーケストレーション・連鎖できます。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: 入力の検証とチェックをエージェントと並行して実行し、チェックが失敗した場合は早期に中断します。
-   セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みのトレーシングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用できます。

## インストール

```bash
pip install openai-agents
```

## Hello World の例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```