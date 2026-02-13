---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) を使用すると、非常に少ない抽象化で、軽量かつ使いやすいパッケージとして、エージェント的な AI アプリを構築できます。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を、本番運用向けにアップグレードしたものです。Agents SDK には、ごく少数の基本コンポーネントがあります。

-   **エージェント**：instructions と tools を備えた LLM
-   **Agents as tools / ハンドオフ**：特定のタスクのために、エージェントが別のエージェントへ委任できる仕組み
-   **ガードレール**：エージェントの入力と出力の検証を可能にする仕組み

これらの基本コンポーネントは、Python と組み合わせることで、ツールとエージェント間の複雑な関係性を表現するのに十分強力であり、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに SDK には、エージェント的フローを可視化してデバッグできる組み込みの **トレーシング** が付属しており、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使用する理由

SDK には、次の 2 つの設計原則があります。

1. 使う価値があるだけの十分な機能を備えつつ、学習を速くするために基本コンポーネントは少なくすること。
2. すぐに使えることに優れつつ、何が起きるかを正確にカスタマイズできること。

以下は、SDK の主な機能です。

-   **エージェントループ**：ツール呼び出しを処理し、結果を LLM に返し、タスクが完了するまで継続する組み込みのエージェントループ。
-   **Python ファースト**：新しい抽象概念を学ぶ必要があるのではなく、組み込みの言語機能を使ってエージェントをオーケストレーションし、連鎖させます。
-   **Agents as tools / ハンドオフ**：複数のエージェント間で作業を調整し、委任するための強力な仕組み。
-   **ガードレール**：エージェントの実行と並行して入力検証と安全性チェックを実行し、チェックに合格しない場合は速やかに失敗させます。
-   **関数ツール**：自動スキーマ生成と Pydantic による検証により、任意の Python 関数をツールに変換します。
-   **MCP server ツール呼び出し**：関数ツールと同じ方法で動作する、組み込みの MCP server ツール統合。
-   **セッション**：エージェントループ内で作業コンテキストを維持するための永続的なメモリレイヤー。
-   **Human in the loop**：エージェント実行全体にわたって人間を関与させるための組み込みの仕組み。
-   **トレーシング**：ワークフローの可視化、デバッグ、監視のための組み込みトレーシング。評価、ファインチューニング、蒸留ツールからなる OpenAI スイートをサポートします。
-   **Realtime Agents**：自動割り込み検出、コンテキスト管理、ガードレールなどの機能を備えた強力な音声エージェントを構築できます。

## インストール

```bash
pip install openai-agents
```

## Hello World 例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください_)

```bash
export OPENAI_API_KEY=sk-...
```