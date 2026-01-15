---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できます。これは、エージェント向けの以前の実験的取り組みである [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクについて、エージェントが他の エージェント に委任できる仕組み
-   **ガードレール**: エージェントの入力と出力を検証する仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に保持する仕組み

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント の複雑な関係を表現でき、学習コストをかけずに実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価を行ったり、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能を備えつつ、学習が早く済むよう基本コンポーネントは少数にする。
2. すぐに使えて優れた体験を提供しつつ、挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツール呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのループ。
-   Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の組み込み機能で エージェント をオーケストレーションして連鎖できます。
-   ハンドオフ: 複数の エージェント 間で調整と委任を行う強力な機能。
-   ガードレール: エージェント と並行して入力の検証やチェックを実行し、チェックに失敗した場合は早期に中断します。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・モニタリングを可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も活用できます。

## インストール

```bash
pip install openai-agents
```

## Hello world の例

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