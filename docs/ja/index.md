---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージにより、エージェント指向の AI アプリを構築できるようにします。本 SDK は、以前にエージェント向けに実験していた [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**：instructions と tools を備えた LLM  
-   **ハンドオフ**：特定のタスクを他のエージェントに委任できる機構  
-   **ガードレール**：エージェントの入力と出力を検証する仕組み  
-   **セッション**：エージェント実行間で会話履歴を自動的に保持  

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現し、急な学習コストなしに実際のアプリケーションを構築できます。さらに、SDK にはワークフローを可視化・デバッグできる組み込みの **トレーシング** が含まれており、評価やファインチューニングにも活用できます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 つです。

1. 使う価値のある十分な機能を提供しつつ、学習が早いように基本コンポーネントを絞る。  
2. デフォルト設定で高いパフォーマンスを発揮しながら、挙動を細かくカスタマイズできる。

主な機能は次のとおりです。

-   エージェントループ：ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを自動で処理。  
-   Python ファースト：新しい抽象概念を学ぶことなく、Python の言語機能でエージェントを編成・連携。  
-   ハンドオフ：複数エージェント間の協調と委任を可能にする強力な機能。  
-   ガードレール：エージェントと並行して入力検証を実行し、失敗時は早期に停止。  
-   セッション：エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要に。  
-   関数ツール：任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。  
-   トレーシング：ワークフローの可視化・デバッグ・モニタリングを行い、OpenAI の評価・ファインチューニング・蒸留ツールも利用可能。  

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

(_このコードを実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```