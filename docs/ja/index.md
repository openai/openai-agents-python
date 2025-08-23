---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できます。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK は、非常に少数の基本コンポーネントを備えています。

-   ** エージェント **: instructions と tools を備えた LLM
-   ** ハンドオフ **: 特定のタスクを他のエージェントに委譲できる仕組み
-   ** ガードレール **: エージェントの入力と出力の検証を可能にする仕組み
-   ** セッション **: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実運用のアプリケーションを構築できます。さらに、この SDK には組み込みの ** トレーシング ** があり、エージェントフローの可視化とデバッグ、評価、そしてアプリケーション向けのモデルの微調整まで行えます。

## Agents SDK を使用する理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能を備えつつ、学習が容易になるよう基本コンポーネントは最小限にする。
2. そのままでも優れた動作をするが、挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整と委譲を強力にサポート。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価、微調整、蒸留ツールを活用可能な組み込みトレーシング。

## インストール

```bash
pip install openai-agents
```

## Hello World のサンプル

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