---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の、本番運用に適したアップグレード版です。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクでエージェントが他のエージェントに委譲できる機能
-   **ガードレール**: エージェントの入力と出力の検証を可能にする機能
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント フローの可視化とデバッグ、評価、さらにはアプリケーション向けのモデルのファインチューニングまで行えます。

## Why use the Agents SDK

この SDK の設計原則は 2 つあります。

1. 十分に使う価値がある機能を備えつつ、基本コンポーネントは少なく、短時間で学べること。
2. そのままでも優れた動作をする一方で、何が起きるかを正確にカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果を LLM へ送信、LLM の完了までのループを処理する組み込みのループ。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整や委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、モニタリングを可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能。

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