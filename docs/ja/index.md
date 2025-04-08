# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、軽量で使いやすく、抽象化を最小限に抑えたエージェントベースの AI アプリケーションを構築できるようにします。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK は非常に少数の基本コンポーネント（primitives）で構成されています。

- **エージェント** : instructions と tools を備えた LLM
- **ハンドオフ** : 特定のタスクを他のエージェントに委任する仕組み
- **ガードレール** : エージェントへの入力を検証する仕組み

これらの基本コンポーネントを Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに SDK には、エージェントのフローを視覚化・デバッグし、評価やモデルのファインチューニングまで可能にする組み込みの **トレーシング** 機能が備わっています。

## Agents SDK を使う理由

SDK は以下の 2 つの設計原則に基づいています。

1. 利用価値のある十分な機能を備えつつ、迅速に習得できるよう基本コンポーネントを最小限に抑える。
2. そのままでも優れた動作をするが、必要に応じて細かくカスタマイズ可能。

SDK の主な機能は以下の通りです。

- **エージェントループ** : ツールの呼び出し、LLM への実行結果の送信、LLM が完了するまでのループ処理を組み込みで提供。
- **Python ファースト** : 新しい抽象化を学ぶ必要なく、Python の言語機能を使ってエージェントの連携やチェーンを構築可能。
- **ハンドオフ** : 複数のエージェント間での調整やタスク委任を可能にする強力な機能。
- **ガードレール** : エージェントと並行して入力の検証やチェックを実行し、チェックが失敗した場合は早期に処理を中断。
- **関数ツール** : 任意の Python 関数をツールに変換し、自動的なスキーマ生成と Pydantic による検証を提供。
- **トレーシング** : ワークフローの視覚化、デバッグ、モニタリングを可能にする組み込みのトレーシング機能。OpenAI が提供する評価、ファインチューニング、蒸留ツールも利用可能。

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

（_実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください。_）

```bash
export OPENAI_API_KEY=sk-...
```