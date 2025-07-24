---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量かつ使いやすいパッケージで エージェント 指向の AI アプリを構築できます。これは、以前にエージェント向けに試験的に公開していた [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK が提供する基本コンポーネントは次のとおりです:

-   **エージェント**: instructions とツールを備えた LLM  
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委任できる  
-   **ガードレール**: エージェントへの入力を検証できる  
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持する  

Python と組み合わせることで、これらの基本コンポーネントだけでツールとエージェント間の複雑な関係を表現でき、急な学習コストなく実運用レベルのアプリケーションを構築できます。さらに、SDK には **トレーシング** が組み込まれており、エージェントのフローを可視化・デバッグできるだけでなく、評価やモデルのファインチューニングにも活用できます。

## Agents SDK を使用する理由

SDK には次の 2 つの設計原則があります:

1. 使う価値のある十分な機能を備えつつ、基本コンポーネントを少数に絞り、学習が迅速に行えること。  
2. 箱から出してすぐに使える一方で、動作を細部までカスタマイズできること。  

SDK の主な機能は次のとおりです:

-   Agent loop: ツール呼び出し、結果を LLM へ送信、LLM が完了するまでのループ処理を行うループが組み込み済み。  
-   Python ファースト: 新しい抽象を学ぶことなく、Python の言語機能でエージェントをオーケストレーションおよび連鎖できます。  
-   ハンドオフ: 複数のエージェント間で調整・委任を行う強力な機能。  
-   ガードレール: エージェントと並列に入力検証やチェックを実行し、チェックに失敗した時点で早期に停止します。  
-   セッション: エージェント実行間の会話履歴を自動で管理し、手動で状態を扱う必要をなくします。  
-   Function tools: どんな Python 関数でもツール化でき、自動的にスキーマを生成し、Pydantic による検証を行います。  
-   トレーシング: ワークフローを可視化・デバッグ・監視できるトレーシングが組み込まれており、OpenAI の評価、ファインチューニング、蒸留ツールも利用できます。  

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

(_これを実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```