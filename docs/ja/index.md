---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント指向の AI アプリを構築できるようにします。これは、当社の過去のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用対応のアップグレード版です。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクについてエージェントが他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化してデバッグし、評価し、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能を備えつつ、学習を素早くするために基本コンポーネントは少数に保つ。
2. すぐに使えて快適に動作しつつ、必要に応じて挙動を正確にカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能でエージェントのオーケストレーションや連携を実現。
-   ハンドオフ: 複数のエージェント間での協調と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツールに変換し、スキーマの自動生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価・ファインチューニング・蒸留ツール群も利用可能な組み込みのトレーシング。

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

( _このコードを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_ )

```bash
export OPENAI_API_KEY=sk-...
```