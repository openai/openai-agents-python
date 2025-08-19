---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できます。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番対応アップグレードです。Agents SDK は非常に少数の基本コンポーネントを提供します:

-   **エージェント**: instructions とツールを備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**: エージェントの入力と出力の検証を可能にする機能
-   **セッション**: エージェントの実行にまたがる会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化してデバッグできるほか、評価し、アプリケーション向けにモデルを微調整することも可能です。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています:

1. 使う価値のある十分な機能を備えつつ、学習が速いよう基本コンポーネントは少数に抑える。
2. そのままでも優れた動作をするが、実際に何が起こるかを正確にカスタマイズできる。

SDK の主な機能は次のとおりです:

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能でエージェントのオーケストレーションと連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗した場合は早期に中断。
-   セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供。
-   トレーシング: フローの可視化・デバッグ・モニタリングを可能にし、OpenAI の評価・微調整・蒸留ツール群も活用できます。

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