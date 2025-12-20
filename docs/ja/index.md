---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化をほとんど用いずに軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、これまでのエージェントに関する実験的取り組みである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用に耐えるアップグレード版です。Agents SDK には、少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクについて、エージェント が他の エージェント に委譲できる機能
-   **ガードレール**: エージェント の入力と出力の検証を可能にする機能
-   **セッション**: エージェント の実行をまたいで会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェント フローの可視化やデバッグに加えて、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Why use the Agents SDK

SDK には、次の 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、学習を速くするために基本コンポーネントは少数に抑える。
2. すぐに使えて高性能でありながら、必要に応じて挙動を正確にカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツール呼び出し、結果を LLM に送信、LLM が完了するまでのループを処理する組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶことなく、言語の組み込み機能で エージェント をオーケストレーションし、連鎖できます。
-   ハンドオフ: 複数の エージェント 間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェント と並行して入力検証やチェックを実行し、チェックに失敗した場合は早期に中断。
-   セッション: エージェント の実行間で会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、スキーマの自動生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みの トレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

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