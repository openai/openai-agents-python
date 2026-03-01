---
search:
  exclude: true
---
# クイックスタート

## プロジェクトと仮想環境の作成

これは一度だけ実行すれば十分です。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 仮想環境の有効化

新しいターミナルセッションを開始するたびにこれを実行してください。

```bash
source .venv/bin/activate
```

### Agents SDK のインストール

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API キーの設定

まだお持ちでない場合は、 OpenAI API キーを作成するために [こちらの手順](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key) に従ってください。

```bash
export OPENAI_API_KEY=sk-...
```

## 最初のエージェントの作成

エージェントは instructions 、名前、および特定のモデルなどの任意の設定で定義します。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 最初のエージェントの実行

[`Runner`][agents.run.Runner] を使用してエージェントを実行し、 [`RunResult`][agents.result.RunResult] を取得します。

```python
import asyncio
from agents import Agent, Runner

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)

async def main():
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

2 回目のターンでは、 `result.to_input_list()` を `Runner.run(...)` に再び渡すか、 [session](sessions/index.md) を関連付けるか、 `conversation_id` / `previous_response_id` を使って OpenAI サーバー管理の状態を再利用できます。 [running agents](running_agents.md) ガイドでは、これらの方法を比較しています。

## エージェントへのツール付与

エージェントには、情報を検索したりアクションを実行したりするためのツールを与えられます。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool
def history_fun_fact() -> str:
    """Return a short history fact."""
    return "Sharks are older than trees."


agent = Agent(
    name="History Tutor",
    instructions="Answer history questions clearly. Use history_fun_fact when it helps.",
    tools=[history_fun_fact],
)


async def main():
    result = await Runner.run(
        agent,
        "Tell me something surprising about ancient life on Earth.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

## 追加エージェントの作成

追加のエージェントも同じ方法で定義できます。 `handoff_description` は、いつ委譲するべきかについてルーティングエージェントに追加のコンテキストを与えます。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You answer history questions clearly and concisely.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You explain math step by step and include worked examples.",
)
```

## ハンドオフの定義

エージェントでは、タスク解決中に選択できる外向きハンドオフオプションの一覧を定義できます。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## エージェントオーケストレーションの実行

ランナーは、個々のエージェントの実行、あらゆるハンドオフ、およびあらゆるツール呼び出しを処理します。

```python
import asyncio
from agents import Runner


async def main():
    result = await Runner.run(
        triage_agent,
        "Who was the first president of the United States?",
    )
    print(result.final_output)
    print(f"Answered by: {result.last_agent.name}")


if __name__ == "__main__":
    asyncio.run(main())
```

## コード例の参照

リポジトリには、同じ主要パターンに対応する完全なスクリプトが含まれています。

-   最初の実行向け: [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)
-   関数ツール向け: [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)
-   マルチエージェントルーティング向け: [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)

## トレースの確認

エージェント実行中に何が起きたかを確認するには、 [OpenAI Dashboard の Trace viewer](https://platform.openai.com/traces) に移動して、エージェント実行のトレースを表示してください。

## 次のステップ

より複雑な agentic フローの構築方法を学びましょう。

-   [Agents](agents.md) の設定方法を学ぶ。
-   [running agents](running_agents.md) と [sessions](sessions/index.md) について学ぶ。
-   [tools](tools.md) 、 [guardrails](guardrails.md) 、 [models](models/index.md) について学ぶ。