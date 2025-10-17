---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント型 AI アプリを構築できるようにするものです。これは、以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応アップグレードです。Agents SDK には非常に小さな基本コンポーネントの集合があります。

-   **エージェント** 、`instructions` とツールを備えた LLM
-   **ハンドオフ** 、エージェントが特定のタスクを他のエージェントに委任できる仕組み
-   **ガードレール** 、エージェントの入力と出力の検証を可能にする仕組み
-   **セッション** 、エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用のアプリケーションを構築できます。加えて、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグし、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK には、次の 2 つの設計原則があります。

1. 使う価値があるだけの十分な機能を備えつつ、学習が速く済むよう基本コンポーネントは少なく。
2. すぐに高品質に動作しつつ、発生する処理を正確にカスタマイズ可能に。

SDK の主な機能は以下のとおりです。

-   エージェント ループ: ツールの呼び出し、実行結果を LLM に送信、LLM の完了までのループを処理する組み込みループ。
-   Python ファースト: 新しい抽象を学ぶことなく、言語の機能を用いてエージェントをオーケストレーション・連結。
-   ハンドオフ: 複数のエージェント間での調整と委任を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、チェックが失敗した場合は早期終了。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動での状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視ができ、OpenAI の評価、微調整、蒸留ツール群も活用可能。

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