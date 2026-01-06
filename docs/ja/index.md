---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化が非常に少ない軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の実運用対応版アップグレードです。Agents SDK には、ごく少数の基本的なコンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクについて、エージェントが他のエージェントへ委任できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実アプリケーションを構築できます。加えて、SDK には組み込みの **トレーシング** が含まれ、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリ向けにモデルを微調整することや、さらにそれらを蒸留することも可能です。

## Agents SDK を使う理由

この SDK は、次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能を備えつつ、学習を素早くするために基本コンポーネントは少数に抑える。
2. そのままでも優れた体験を提供しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、LLM への結果送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象化を学ぶことなく、組み込みの言語機能でエージェントをオーケストレーションし、連鎖できます。
-   ハンドオフ: 複数のエージェント間での調整と委任を可能にする強力な機能。
-   ガードレール: 入力の検証やチェックをエージェントと並行して実行し、チェックに失敗した場合は早期に中断します。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、スキーマの自動生成と Pydantic ベースの検証を提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、さらに OpenAI の評価・微調整・蒸留ツール群を利用できます。

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