---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用に適したアップグレード版です。Agents SDK には、ごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力を検証する仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持する仕組み

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現するのに十分強力であり、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化してデバッグできるほか、評価したり、アプリケーション向けにモデルを微調整することも可能です。

## Agents SDK を使う理由

この SDK には 2 つの設計原則があります。

1. 使う価値がある十分な機能を備えつつ、学習が迅速に済むよう基本コンポーネントは少数に抑える。
2. すぐに使えて十分に機能する一方で、実際に何が起きるかを正確にカスタマイズできる。

SDK の主な特徴は次のとおりです。

-   エージェント ループ: ツールの呼び出し、実行結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントをオーケストレーションし連鎖させる。
-   ハンドオフ: 複数のエージェント間で調整と委譲を行う強力な機能。
-   ガードレール: エージェントと並行して入力のバリデーションやチェックを実行し、チェックに失敗した場合は早期に中断。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動の状態管理を不要にする。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションを提供。
-   トレーシング: ワークフローの可視化、デバッグ、モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群の利用を可能にする組み込みのトレーシング。

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