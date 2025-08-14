---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化が非常に少ない、軽量で使いやすいパッケージで、エージェント型 AI アプリの構築を可能にします。これは、エージェント に関する以前の実験である [Swarm](https://github.com/openai/swarm/tree/main) をプロダクション対応にアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります:

-   ** エージェント **、instructions と tools を備えた LLM
-   ** ハンドオフ **、エージェント が特定のタスクを他の エージェント に委譲できるようにする
-   ** ガードレール **、エージェント の入力と出力の検証を可能にする
-   ** セッション **、エージェント の実行全体で会話履歴を自動的に維持する

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント 間の複雑な関係を表現できるほど強力になり、急な学習曲線なしに実運用アプリケーションを構築できます。さらに、この SDK には組み込みの  **トレーシング**  が付属しており、エージェント フローの可視化やデバッグ、評価に加えて、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 つです:

1. 使う価値があるだけの機能は揃えつつ、基本コンポーネントを最小限にして素早く学べます。
2. すぐにうまく動作しますが、実際に何が起きるかを正確にカスタマイズできます。

この SDK の主な機能は次のとおりです:

-   エージェント ループ: ツールの呼び出し、結果を LLM に送る処理、そして LLM が完了するまでのループを扱う組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、組み込みの言語機能で エージェント をオーケストレーションし、連鎖させられます。
-   ハンドオフ: 複数の エージェント 間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェント と並行して入力のバリデーションやチェックを実行し、失敗時は早期に中断。
-   セッション: エージェント の実行をまたいだ会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic によるバリデーションを提供。
-   トレーシング: ワークフローの可視化・デバッグ・モニタリングを可能にする組み込みのトレーシングに加え、評価、ファインチューニング、蒸留ツールを含む OpenAI のスイートを利用可能。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください_)

```bash
export OPENAI_API_KEY=sk-...
```