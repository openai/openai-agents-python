---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) を使用すると、抽象化を最小限に抑えた軽量で扱いやすいパッケージで、エージェント指向の AI アプリを構築できます。これは、以前のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番利用できるレベルへとアップグレードしたものです。Agents SDK には、基本コンポーネントがごくわずかしかありません。

-   **エージェント**: instructions と tools を備えた LLM です  
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントへ委任できるしくみ  
-   **ガードレール**: エージェントへの入力を検証する機能  
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持します  

Python と組み合わせることで、これらの基本コンポーネントは tools とエージェント間の複雑な関係を表現できるだけの十分な力を持ち、急な学習コストなしに実用アプリを構築できます。さらに、SDK には組み込みの トレーシング があり、エージェントのフローを可視化・デバッグできるほか、評価やアプリ向けモデルのファインチューニングにも活用できます。

## Agents SDK を使う理由

SDK には、次の 2 つの設計原則があります:

1. 利用価値のある十分な機能を持ちながら、基本コンポーネントを絞り込むことで学習がすばやく済むこと。  
2. すぐに使えて高い性能を発揮しつつ、挙動を細部までカスタマイズできること。  

SDK の主な機能は次のとおりです:

-   Agent ループ: tools の呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを自動で実行します。  
-   Python ファースト: 新しい抽象を覚えることなく、言語が元々備える機能でエージェントをオーケストレーションし、チェーンできます。  
-   ハンドオフ: 複数のエージェント間の連携と委任を可能にする強力な機能です。  
-   ガードレール: エージェントと並行して入力の検証を行い、チェックに失敗した場合は早期に処理を終了します。  
-   セッション: エージェントの実行をまたいで会話履歴を自動管理し、状態を手動で扱う手間を排除します。  
-   関数ツール: 任意の Python 関数を tools に変換し、スキーマを自動生成して Pydantic ベースの検証を行います。  
-   トレーシング: フローの可視化、デバッグ、モニタリングが行える組み込み機能。さらに OpenAI の評価、ファインチューニング、蒸留ツール群も利用できます。  

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

(_実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```