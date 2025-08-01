---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) を使うと、ごくわずかな抽象化で軽量かつ使いやすいパッケージとして、エージェント指向の AI アプリを構築できます。これは以前にエージェント向けに行った実験的プロジェクト [Swarm](https://github.com/openai/swarm/tree/main) を、本番環境向けに強化したアップグレード版です。Agents SDK には、非常に小さな基本コンポーネントセットが含まれています。

-   **Agents**: instructions と tools を備えた LLM  
-   **Handoffs**: 特定のタスクを他のエージェントに委任できる機能  
-   **Guardrails**: エージェントへの入力を検証する仕組み  
-   **Sessions**: エージェント実行間で会話履歴を自動管理する機能  

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなく実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント フローの可視化・デバッグ・評価、さらにはモデルのファインチューニングまで行えます。

## Agents SDK を利用する理由

SDK には次の 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、学習が早いように基本コンポーネントを最小限にする。  
2. デフォルトで快適に動作しつつ、挙動を細かくカスタマイズできる。  

主な機能は以下のとおりです。

-   Agent loop: tools の呼び出し、LLM への結果送信、LLM が完了するまでのループ処理を担う組み込みエージェント ループ。  
-   Python ファースト: 新しい抽象化を学ばずに、言語そのものの機能でエージェントを編成・連結可能。  
-   Handoffs: 複数エージェント間で調整・委任を行う強力な機能。  
-   Guardrails: エージェントと並行して入力検証を実行し、失敗時には早期に処理を打ち切り。  
-   Sessions: エージェント実行間の会話履歴を自動管理し、手動での状態管理を不要に。  
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic での検証を提供。  
-   トレーシング: ワークフローの可視化・デバッグ・モニタリングに加え、OpenAI の評価・ファインチューニング・蒸留ツールを活用可能。  

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

(_実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```