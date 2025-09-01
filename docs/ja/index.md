---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント指向の AI アプリを構築できます。これは、以前のエージェントに関する実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレードです。Agents SDK には、非常に少数の基本コンポーネントがあります:

-   ** エージェント **: instructions と tools を備えた LLM
-   ** ハンドオフ **: 特定のタスクについて、エージェントが他のエージェントに委任できる仕組み
-   ** ガードレール **: エージェントの入力と出力を検証できる仕組み
-   ** セッション **: エージェント実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの ** トレーシング ** が含まれており、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリケーション向けにモデルを微調整することもできます。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの十分な機能を備えつつ、学習が素早く済むよう基本コンポーネントは少数にする。
2. デフォルトでそのまま高い性能で動作しつつ、動作内容を細かくカスタマイズできる。

主な機能は以下のとおりです。

-   エージェントループ: ツール呼び出し、LLM への結果送信、LLM が完了するまでのループ処理を行う組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能でエージェントをオーケストレーションし、連鎖できます。
-   ハンドオフ: 複数のエージェント間の調整と委任を行う強力な機能。
-   ガードレール: 入力の検証とチェックをエージェントと並行して実行し、チェックが失敗した場合は早期に打ち切ります。
-   セッション: エージェントの実行をまたいで会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、スキーマ自動生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みトレーシングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

## インストール

```bash
pip install openai-agents
```

## Hello world の例

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