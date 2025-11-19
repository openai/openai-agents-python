---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント 型の AI アプリを構築できるようにします。これは以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版です。Agents SDK には、ごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他の エージェント に委任できる仕組み
- **ガードレール**: エージェント の入力と出力を検証する仕組み
- **セッション**: エージェント 実行間で会話履歴を自動管理

Python と組み合わせることで、これらの基本コンポーネントは tools と エージェント の複雑な関係を表現でき、学習コストを抑えつつ実運用レベルのアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェント フローの可視化・デバッグ・評価に加えて、アプリケーション向けにモデルのファインチューニングも行えます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能は備えるが、学習を素早くするため基本コンポーネントは少数に保つ。
2. そのままでも優れた動作をするが、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェント ループ: tools の呼び出し、結果の LLM への送信、LLM の完了までのループを処理する組み込みループ。
- Python ファースト: 新しい抽象化を学ぶのではなく、言語の標準機能で エージェント のオーケストレーションや連鎖を実現。
- ハンドオフ: 複数の エージェント 間での調整と委任を可能にする強力な機能。
- ガードレール: エージェント と並行して入力の検証やチェックを実行し、失敗時には早期中断。
- セッション: エージェント 実行間での会話履歴を自動管理し、手動での状態管理を不要に。
- 関数ツール: 任意の Python 関数を tool に変換し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツール群を活用できる組み込みのトレーシング。

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