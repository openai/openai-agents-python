---
search:
  exclude: true
---
# OpenAI Agents SDK

 OpenAI Agents SDK は、極めて少ない抽象化で軽量かつ使いやすいパッケージを通じて、エージェント指向の AI アプリを構築できるようにします。これは、以前にエージェント向けに実験していた  Swarm の本番運用向けアップグレード版です。 Agents SDK には、ごく限られた基本コンポーネントしかありません。

-   **Agents**: LLM に instructions と tools を装備したもの  
-   **Handoffs**: 特定のタスクを他のエージェントに委任できる仕組み  
-   **Guardrails**: エージェントへの入力を検証する仕組み  
-   **Sessions**: エージェント実行間で会話履歴を自動的に保持する機能  

 Python と組み合わせることで、これらの基本コンポーネントは tool とエージェント間の複雑な関係を表現するのに十分な強力さを持ち、急な学習コストなしに実際のアプリケーションを構築できます。さらに、SDK には組み込みの **tracing** があり、エージェントフローを可視化・デバッグできるほか、評価やファインチューニングにも活用できます。

## Agents SDK を使用する理由

SDK の設計方針は 2 つです。

1. 使う価値のある十分な機能を持ちつつ、学習が早いように基本コンポーネントを最小限にする。  
2. デフォルト設定で高い実用性を確保しつつ、挙動を細部までカスタマイズできる。  

主な機能は次のとおりです。

-   Agent loop: tool の呼び出し、結果を LLM へ送信、LLM が完了するまでのループ処理を自動で実行。  
-    Python ファースト: 新しい抽象化を学ぶことなく、言語本来の機能でエージェントを連携・オーケストレーション。  
-   Handoffs: 複数エージェント間の調整と委任を行う強力な機能。  
-   Guardrails: 入力検証やチェックをエージェントと並行して実行し、失敗時には早期終了。  
-   Sessions: エージェント実行間の会話履歴を自動管理し、手動での状態管理を不要に。  
-   Function tools: どんな  Python 関数でも tool に変換し、自動スキーマ生成と Pydantic ベースの検証を提供。  
-   Tracing: フローの可視化・デバッグ・監視を行い、OpenAI の評価、ファインチューニング、蒸留ツールを活用可能。  

## Installation

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

(_実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```