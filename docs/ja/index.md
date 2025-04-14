# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で、軽量かつ使いやすいパッケージで エージェント 型 AI アプリを構築できる SDK です。これは、以前のエージェント向け実験プロジェクト [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレード版です。Agents SDK には、非常に少数の基本コンポーネントが含まれています。

-   **エージェント**： instructions と tools を備えた LLM
-   **ハンドオフ**：特定のタスクを他のエージェントに委任できる仕組み
-   **ガードレール**：エージェントへの入力を検証できる仕組み

Python と組み合わせることで、これらの基本コンポーネントは tools と エージェント 間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** 機能があり、エージェント フローの可視化やデバッグ、評価、さらにはアプリケーション向けモデルのファインチューニングも可能です。

## なぜ Agents SDK を使うのか

SDK には 2 つの設計原則があります。

1. 使う価値のある十分な機能を持ちつつ、学習が容易なほど少ない基本コンポーネントで構成されていること。
2. すぐに使い始められるが、動作を細かくカスタマイズできること。

SDK の主な特徴は以下の通りです。

-   エージェントループ： tools の呼び出し、LLM への実行結果の送信、LLM が完了するまでのループ処理を自動で行います。
-   Python ファースト：新しい抽象化を学ぶ必要なく、Python の言語機能でエージェントのオーケストレーションや連携が可能です。
-   ハンドオフ：複数のエージェント間での調整や委任を実現する強力な機能です。
-   ガードレール：エージェントと並行して入力検証やチェックを実行し、チェックに失敗した場合は早期に処理を中断します。
-   関数ツール：任意の Python 関数を自動でスキーマ生成・Pydantic ベースのバリデーション付きツールに変換できます。
-   トレーシング：組み込みのトレーシングでワークフローの可視化・デバッグ・モニタリングができ、OpenAI の評価・ファインチューニング・蒸留ツールも利用可能です。

## インストール

```bash
pip install openai-agents
```

## Hello World サンプル

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```