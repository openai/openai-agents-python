# OpenAI エージェント SDK

[OpenAI エージェント SDK](https://github.com/openai/openai-agents-python) は、軽量で使いやすいパッケージでエージェント AI アプリを構築することを可能にします。これは、以前のエージェントの実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応のアップグレード版です。エージェント SDK には、非常に少ない基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委任することを可能にします
- **ガードレール**: エージェントへの入力を検証することを可能にします

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現するのに十分な強力さを持ち、急な学習曲線なしで実際のアプリケーションを構築することができます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを視覚化してデバッグし、評価し、さらにはアプリケーション用にモデルを微調整することができます。

## なぜエージェント SDK を使うのか

SDK には 2 つの設計原則があります。

1. 使用する価値があるだけの機能を持ち、学習が迅速になるように基本コンポーネントを少なくする。
2. すぐに使えるが、何が起こるかを正確にカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、LLM への結果の送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象化を学ぶ必要なく、組み込みの言語機能を使用してエージェントを編成し、連鎖させる。
- ハンドオフ: 複数のエージェント間で調整し、委任するための強力な機能。
- ガードレール: エージェントと並行して入力検証とチェックを実行し、チェックが失敗した場合は早期に中断。
- 関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic による検証を行う。
- トレーシング: ワークフローを視覚化、デバッグ、監視するための組み込みのトレーシング、および OpenAI の評価、微調整、蒸留ツールを使用。

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