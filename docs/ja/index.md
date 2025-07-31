---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、わずかな抽象化で軽量かつ使いやすいパッケージとして エージェント 型 AI アプリを構築できる SDK です。これは、以前にエージェント 用に実験的に提供していた [Swarm](https://github.com/openai/swarm/tree/main) をプロダクション向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります:

-   **エージェント**、instructions と tools を備えた LLM  
-   **ハンドオフ**、特定のタスクを他の エージェント に委任できる仕組み  
-   **ガードレール**、エージェント への入力を検証する機能  
-   **セッション**、エージェント の実行をまたいで会話履歴を自動的に保持  

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント の複雑な関係を表現するのに十分強力であり、急な学習曲線なしに実用的なアプリケーションを構築できます。さらに SDK には組み込みの **トレーシング** が付属しており、エージェント フローを可視化してデバッグしたり、評価やモデルのファインチューニングに利用したりできます。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります:

1. 使う価値があるだけの機能を備えつつ、学習が速いように基本コンポーネントを最小限にすること  
2. デフォルトで快適に動作しつつ、挙動を細部までカスタマイズできること  

SDK の主な機能は次のとおりです:

-   エージェントループ: ツールの呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを内蔵  
-   Python ファースト: 新しい抽象を学ばずに、Python の言語機能だけで エージェント をオーケストレーション・連鎖  
-   ハンドオフ: 複数の エージェント 間でタスクを調整・委任する強力な機能  
-   ガードレール: エージェント と並行して入力検証を実行し、チェックに失敗した場合は早期に停止  
-   セッション: エージェント の実行をまたいで会話履歴を自動管理し、手動の状態管理を不要に  
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションを提供  
-   トレーシング: フローを可視化・デバッグ・モニタリングでき、OpenAI の評価・ファインチューニング・蒸留ツールを活用可能  

## インストール

```bash
pip install openai-agents
```

## Hello World のコード例

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