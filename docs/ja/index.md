---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化が非常に少ない軽量で使いやすいパッケージで、エージェント 指向の AI アプリを構築できるようにします。これは、以前のエージェント 向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレードです。Agents SDK には、ごく少数の基本的な構成要素があります。

-   ** エージェント **、LLM に instructions と tools を備えたもの
-   ** ハンドオフ **、特定のタスクについて他のエージェント へ委譲できる仕組み
-   ** ガードレール **、エージェント の入力と出力の検証を可能にするもの
-   ** セッション **、エージェント 実行間で会話履歴を自動的に維持するもの

Python と組み合わせることで、これらの基本的な構成要素はツールとエージェント 間の複雑な関係を表現でき、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに、SDK には組み込みの ** トレーシング ** が付属しており、エージェント のフローを可視化してデバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

SDK の設計原則は 2 つあります。

1. 使う価値があるだけの機能は備えつつ、学習を速くするために基本的な構成要素は少数にとどめる。
2. そのままでも高性能に動作しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツール呼び出し、結果を LLM へ送信、LLM の完了までのループを自動で処理します。
-   Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の組み込み機能でエージェント をオーケストレーションし、連携・連鎖できます。
-   ハンドオフ: 複数のエージェント 間での調整と委譲を実現する強力な機能です。
-   ガードレール: エージェント と並行して入力のバリデーションやチェックを実行し、失敗時には早期に打ち切ります。
-   セッション: エージェント 実行間の会話履歴を自動管理し、手動での状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、schema の自動生成と Pydantic ベースのバリデーションを提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視が可能な組み込みのトレーシングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用できます。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```