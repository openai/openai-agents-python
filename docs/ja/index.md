---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化をほとんど用いずに軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験 [Swarm](https://github.com/openai/swarm/tree/main) を本番運用可能な形にアップグレードしたものです。Agents SDK はごく少数の基本コンポーネントから成ります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントへ委譲できる機能
-   **ガードレール**: エージェントの入力・出力の検証を可能にする機能
-   **セッション**: エージェントの実行間で会話履歴を自動的に保持する仕組み

これらの基本コンポーネントは、Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価やアプリケーション向けのモデルのファインチューニングも可能です。

## Agents SDK を使う理由

SDK の設計方針は次の 2 点です。

1. 使う価値のある十分な機能を備えつつ、学習が容易なように基本コンポーネントは少数に保つこと。
2. そのまま使っても優れた体験を提供しつつ、挙動を細かくカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループを内蔵で処理。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の標準機能でエージェントのオーケストレーションや連携を実現。
-   ハンドオフ: 複数エージェント間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動での状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

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