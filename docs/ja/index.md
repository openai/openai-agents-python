---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとしてエージェント型 AI アプリを構築できるツールです。これは、以前のエージェント実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の実装を、実運用向けにアップグレードしたものです。本 SDK には、ごく少数の基本コンポーネントが含まれます。

-   **エージェント** 、指示とツールを備えた LLM  
-   **ハンドオフ** 、特定タスクを他のエージェントに委任する仕組み  
-   **ガードレール** 、エージェントへの入力を検証する機能  
-   **セッション** 、エージェント実行間で会話履歴を自動管理する仕組み  

Python と組み合わせることで、これらのコンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに SDK には **トレーシング** が標準搭載されており、エージェントフローの可視化・デバッグに加え、評価やモデルのファインチューニングにも活用できます。

## Agents SDK の利点

本 SDK は、次の 2 つの設計原則に基づいています。

1. 使用する価値のある十分な機能を備えつつ、学習が容易になるようコンポーネントを絞り込む。  
2. デフォルト設定で高い利便性を提供しつつ、必要に応じて挙動を細かくカスタマイズできる。  

主な機能は次のとおりです。

-   エージェントループ: ツール呼び出し、結果の LLM への送信、完了までのループ処理を自動化。  
-   Python ファースト: 新しい抽象概念を覚える必要なく、Python の言語機能でエージェントをオーケストレーション。  
-   ハンドオフ: 複数エージェント間の調整と委任を可能にする強力な機能。  
-   ガードレール: 入力検証をエージェントと並行して実行し、失敗時には即座に中断。  
-   セッション: エージェント実行間の会話履歴を自動管理し、手動での状態保持を不要化。  
-   関数ツール: 任意の Python 関数をツール化し、スキーマ生成と Pydantic ベースのバリデーションを自動実施。  
-   トレーシング: フローの可視化・デバッグ・モニタリングに加え、OpenAI の評価・ファインチューニング・蒸留ツールを活用可能。  

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

(_実行する際は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```