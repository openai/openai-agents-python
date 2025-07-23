---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化でエージェント的な AI アプリを軽量かつ簡単に構築できるパッケージです。これは、以前のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントがあります:

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定タスクを他のエージェントに委任する仕組み
-   **ガードレール**: エージェントへの入力を検証する仕組み
-   **セッション**: エージェント実行間で会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントフローの可視化とデバッグに加えて、評価やモデルのファインチューニングまで行えます。

## Agents SDK を使用する理由

SDK には 2 つの設計原則があります:

1. 使う価値のある十分な機能を持ちつつ、学習コストが低い最小限の基本コンポーネント。
2. デフォルトで優れた動作を提供しつつ、挙動を自由にカスタマイズできること。

主な機能は次のとおりです:

-   Agent loop: ツールの呼び出し、実行結果を LLM へ渡し、LLM が完了するまでループする処理を組み込みで提供します。
-   Python ファースト: 新しい抽象を学ぶ必要なく、Python の言語機能だけでエージェントをオーケストレーションして連鎖できます。
-   Handoffs: 複数エージェント間での調整と委任を可能にする強力な機能。
-   Guardrails: エージェントと並行して入力バリデーションやチェックを実行し、失敗時には早期終了します。
-   Sessions: エージェント実行をまたいで会話履歴を自動管理し、手動の状態管理を排除します。
-   Function tools: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションを提供します。
-   Tracing: ワークフローの可視化、デバッグ、モニタリングができ、OpenAI の評価・ファインチューニング・蒸留ツール群とも連携します。

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

 (_実行する際は、`OPENAI_API_KEY` 環境変数を設定してください_) 

```bash
export OPENAI_API_KEY=sk-...
```