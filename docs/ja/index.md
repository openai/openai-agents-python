---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにするものです。これは、エージェントに関する以前の実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレードです。Agents SDK にはごく少数の基本 components があります。

-   **エージェント** 、instructions と tools を備えた LLM
-   **ハンドオフ** 、特定のタスクでエージェントが他のエージェントに委譲できる機能
-   **ガードレール** 、エージェントの入力と出力の検証を可能にする機能
-   **セッション** 、エージェントの実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本 components はツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属し、エージェントのフローを可視化・デバッグできるほか、評価やアプリ向けのモデルの微調整も行えます。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります。

1. 使う価値があるだけの機能は備えつつ、学習を素早くするために基本 components は少数に抑える。
2. そのままでも十分に動作し、必要に応じて挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果を LLM に送る処理、LLM が完了するまでのループを処理する組み込みループ。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントをオーケストレーションし連鎖させます。
-   ハンドオフ: 複数のエージェント間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力のバリデーションやチェックを実行し、チェックが失敗したら早期に中断します。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みのトレーシングに加え、 OpenAI の評価、ファインチューニング、蒸留ツールのスイートを利用できます。

## インストール

```bash
pip install openai-agents
```

## Hello World example

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