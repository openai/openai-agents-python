---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの挙動をすばやく対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、各ターン間で会話履歴を保持します。デフォルトでは、生成と同時にモデル出力をストリーミングします。上記の例を実行すると、`run_demo_loop` は対話型チャットセッションを開始します。あなたの入力を継続的に求め、各ターン間で会話全体の履歴を記憶し（そのためエージェントは何が話されたかを把握できます）、生成と同時にエージェントの応答をリアルタイムで自動ストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、キーボードショートカットの  Ctrl-D  を使用します。