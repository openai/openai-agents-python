---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化の少ない軽量で使いやすいパッケージで エージェント 型の AI アプリを構築できるようにします。これは、以前の エージェント 向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります:

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクについて、エージェント が他の エージェント に委譲できる機能
-   **ガードレール**: エージェント の入力と出力の検証を可能にする機能
-   **セッション**: エージェント の実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントは ツール と エージェント の複雑な関係を表現でき、急な学習曲線なしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント のフローを可視化・デバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります:

1. 使う価値がある十分な機能を備えつつ、学習が速いように基本コンポーネントは少数にする。
2. すぐに使える一方で、挙動を正確にカスタマイズできる。

SDK の主な機能は次のとおりです:

-   エージェント ループ: ツール呼び出し、結果を LLM へ送信、LLM の完了までのループ処理を行う組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能で エージェント のオーケストレーションや連携が可能。
-   ハンドオフ: 複数の エージェント 間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェント と並列で入力のバリデーションやチェックを実行し、失敗時には早期に中断。
-   セッション: エージェント の実行間で会話履歴を自動管理し、手動での状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能な組み込みのトレーシング。

## インストール

```bash
pip install openai-agents
```

## Hello World のサンプルコード

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