---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化がごく少なく軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるツールです。以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番運用レベルにアップグレードしたものです。Agents SDK はごく少数の基本コンポーネントを備えています。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲する仕組み
-   **ガードレール**: エージェントの入力・出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持します

Python と組み合わせることで、これらの基本コンポーネントだけでツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価やアプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能は備えるが、学習を素早くするため基本コンポーネントは少数に保つ。
2. そのままでも優れた体験を提供しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを内蔵で処理します。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語機能を用いてエージェントをオーケストレーションし連携させます。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能です。
-   ガードレール: エージェントと並行して入力検証とチェックを実行し、失敗した場合は早期に打ち切ります。
-   セッション: エージェントの実行をまたいだ会話履歴の管理を自動化し、手動での状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic による検証を提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツールのスイートも活用できます。

## インストール

```bash
pip install openai-agents
```

## Hello World のコード例

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