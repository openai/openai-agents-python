---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番対応へとアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント** 、instructions と tools を備えた LLM
-   **ハンドオフ** 、特定のタスクで他のエージェントへ委譲できる仕組み
-   **ガードレール** 、エージェントの入力と出力を検証できる仕組み
-   **セッション** 、エージェントの実行間で会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリ向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能は備える一方、学習が速いよう基本コンポーネントは少数に保つ。
2. すぐに使えて高性能だが、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツール呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を内蔵。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語機能でエージェントをオーケストレーションし連鎖できます。
-   ハンドオフ: 複数エージェント間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力のバリデーションやチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動での状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を利用可能。

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

( _これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_ )

```bash
export OPENAI_API_KEY=sk-...
```