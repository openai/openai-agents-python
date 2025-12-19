---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する当社の以前の実験的取り組みである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレード版です。Agents SDK はごく少数の基本コンポーネントで構成されています:

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、きつい学習コストなしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントの処理フローの可視化とデバッグに加えて、評価やアプリケーション向けのモデル微調整まで行えます。

## Agents SDK を使う理由

SDK は次の 2 つの設計原則に基づいています:

1. 使う価値があるだけの機能を備えつつ、学習が素早く済むよう基本コンポーネントは少数に保つ。
2. そのままでも高品質に動作しつつ、挙動を細部までカスタマイズ可能にする。

SDK の主な機能は次のとおりです:

-   エージェント ループ: ツールの呼び出し、結果を LLM へ送信、LLM の完了までのループを処理する組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の組み込み機能でエージェントをオーケストレーションし連携できます。
-   ハンドオフ: 複数のエージェント間で調整と委譲を行う強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行をまたいだ会話履歴の自動管理により、手動の状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、スキーマ自動生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みのトレーシングに加え、OpenAI の評価・微調整・蒸留ツール群を活用可能。

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