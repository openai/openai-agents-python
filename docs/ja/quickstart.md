---
search:
  exclude: true
---
# クイックスタート

## プロジェクトと仮想環境の作成

これは一度だけ行えば十分です。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 仮想環境の有効化

新しいターミナルセッションを開始するたびに行ってください。

macOS または Linux の場合:

```bash
source .venv/bin/activate
```

Windows の場合:

```cmd
.venv\Scripts\activate
```

### Agents SDK のインストール

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API キーの設定

まだ持っていない場合は、[こちらの手順](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)に従って OpenAI API キーを作成してください。

以下のコマンドは、現在のターミナルセッションにキーを設定します。

macOS または Linux の場合:

```bash
export OPENAI_API_KEY=sk-...
```

Windows PowerShell の場合:

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Windows コマンドプロンプトの場合:

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 最初のエージェントの作成

エージェントは、instructions、名前、および特定のモデルなどの任意の設定で定義します。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 最初のエージェントの実行

[`Runner`][agents.run.Runner] を使用してエージェントを実行し、[`RunResult`][agents.result.RunResult] を取得します。

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

2 回目のターンでは、`result.to_input_list()` を `Runner.run(...)` に戻して渡すか、[セッション](sessions/index.md)をアタッチするか、`conversation_id` / `previous_response_id` で OpenAI によりサーバー側で管理される状態を再利用できます。[エージェントの実行](running_agents.md)ガイドでは、これらのアプローチを比較しています。

次の目安を使ってください:

| 実現したいこと... | まず使うもの... |
| --- | --- |
| 完全な手動制御とプロバイダー非依存の履歴 | `result.to_input_list()` |
| SDK に履歴の読み込みと保存を任せる | [`session=...`](sessions/index.md) |
| OpenAI 管理のサーバー側継続 | `previous_response_id` または `conversation_id` |

トレードオフと正確な挙動については、[エージェントの実行](running_agents.md#choose-a-memory-strategy)を参照してください。

タスクが主にプロンプト、ツール、会話状態で完結する場合は、シンプルな `Agent` と `Runner` を使います。エージェントが分離されたワークスペース内の実ファイルを検査または変更する必要がある場合は、[Sandbox エージェントのクイックスタート](sandbox_agents.md)に進んでください。

## エージェントへのツールの付与

エージェントにツールを与えることで、情報を調べたりアクションを実行したりできます。

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

## さらにいくつかのエージェントの追加

マルチエージェントパターンを選ぶ前に、最終回答の主導権を誰が持つべきかを決めてください。

-   **ハンドオフ**: スペシャリストが、そのターンの該当部分について会話を引き継ぎます。
-   **Agents as tools**: オーケストレーターが制御を維持し、スペシャリストをツールとして呼び出します。

このクイックスタートでは、最初の例として最も短いため、 **ハンドオフ** で続けます。マネージャースタイルのパターンについては、[エージェントオーケストレーション](multi_agent.md)と[ツール: agents as tools](tools.md#agents-as-tools)を参照してください。

追加のエージェントも同じ方法で定義できます。`handoff_description` は、いつ委譲すべきかについて、ルーティングエージェントに追加のコンテキストを提供します。

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

エージェントには、タスクを解決する際に選択できるハンドオフ先の選択肢の一覧を定義できます。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## エージェントオーケストレーションの実行

ランナーは、個々のエージェントの実行、すべてのハンドオフ、すべてのツール呼び出しを処理します。

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

## 参考コード例

このリポジトリには、同じ主要パターンに対応する完全なスクリプトが含まれています:

-   [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py) は最初の実行の例です。
-   [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py) は関数ツールの例です。
-   [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py) はマルチエージェントルーティングの例です。

## トレースの表示

エージェントの実行中に何が起きたかを確認するには、[OpenAI ダッシュボードのトレースビューアー](https://platform.openai.com/traces)に移動して、エージェント実行のトレースを表示してください。

## 次のステップ

より複雑なエージェント型フローの構築方法を学びましょう:

-   [エージェント](agents.md)の設定方法について学びます。
-   [エージェントの実行](running_agents.md)と[セッション](sessions/index.md)について学びます。
-   作業を実際のワークスペース内で行う必要がある場合は、[Sandbox エージェント](sandbox_agents.md)について学びます。
-   [ツール](tools.md)、[ガードレール](guardrails.md)、[モデル](models/index.md)について学びます。