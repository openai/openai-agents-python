---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント指向の AI アプリを構築できるようにします。これは従来のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を、プロダクション利用に対応させたアップグレード版です。Agents SDK には、ごく少数の基本コンポーネントが含まれています。

-   **エージェント**: instructions と tools を備えた LLM  
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委任できます  
-   **ガードレール**: エージェントの入力・出力を検証できます  
-   **セッション**: エージェント実行間の会話履歴を自動的に保持します  

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント フローを可視化・デバッグし、評価やモデルのファインチューニングにも活用できます。

## Agents SDK を使う理由

SDK は次の 2 点を設計原則としています。

1. 機能は十分だが、学習するべきコンポーネントは少ない。  
2. デフォルト設定で高い実用性を実現しつつ、動作を細かくカスタマイズできる。  

主な機能は以下のとおりです。

-   エージェント ループ: tools の呼び出し、結果の LLM への送信、LLM が終了するまでのループ処理を自動で行います。  
-   Python ファースト: 新たな抽象を学ばなくても、組み込み言語機能でエージェントをオーケストレーションおよびチェーンできます。  
-   ハンドオフ: 複数のエージェント間で調整と委任を行う強力な機能です。  
-   ガードレール: エージェントと並行して入力検証を行い、失敗時には早期に中断します。  
-   セッション: エージェント実行間で会話履歴を自動管理し、手動での状態管理を不要にします。  
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供します。  
-   トレーシング: ワークフローを可視化・デバッグ・モニタリングでき、 OpenAI の評価・ファインチューニング・蒸留ツールも利用できます。  

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

（これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください）

```bash
export OPENAI_API_KEY=sk-...
```