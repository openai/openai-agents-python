---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版アップグレードです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   ** エージェント **: instructions と tools を備えた LLM
-   ** ハンドオフ **: 特定のタスクについて、エージェントが他のエージェントへ委譲できる機能
-   ** ガードレール **: エージェントの入力と出力の検証を可能にする機能
-   ** セッション **: エージェントの実行間で会話履歴を自動的に保持する機能

これらの基本コンポーネントは、 Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに、 SDK には組み込みの ** トレーシング ** が含まれており、エージェントのフローを可視化・デバッグし、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は、次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの十分な機能を備えつつ、学習がすばやく済むよう基本コンポーネントは少数にする。
2. すぐに使えて優れた体験を提供しつつ、動作を細部までカスタマイズできる。

主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、 LLM の完了までのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象化を学ぶ必要なく、言語の組み込み機能でエージェントのオーケストレーションとチェーン化が可能。
-   ハンドオフ: 複数のエージェント間の調整と委譲を可能にする強力な機能。
-   ガードレール: 検証をエージェントと並行して実行し、チェックが失敗したら早期に打ち切り。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、 OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能。

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