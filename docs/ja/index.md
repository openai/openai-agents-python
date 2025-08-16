---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、当社のエージェントに関するこれまでの実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版アップグレードです。Agents SDK にはごく少数の基本的なコンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲する仕組み
-   **ガードレール**: エージェントの入力と出力を検証する機能
-   **セッション**: エージェントの実行間で会話履歴を自動的に保持

これらの基本的なコンポーネントは、Python と組み合わせることで、tools とエージェント間の複雑な関係を表現でき、急な学習曲線なしで実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリケーション向けにモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、学習が早いように基本要素は少なくすること。
2. すぐに高い性能で使える一方で、挙動を細かくカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果を LLM に渡す処理、LLM が完了するまでのループを担う組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を覚えるのではなく、言語の組み込み機能でエージェントをオーケストレーションおよび連鎖。
-   ハンドオフ: 複数のエージェント間で調整・委譲できる強力な機能。
-   ガードレール: エージェントと並行して入力のバリデーションやチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動での状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツール群を利用可能な組み込みトレーシング。

## インストール

```bash
pip install openai-agents
```

## Hello world example

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