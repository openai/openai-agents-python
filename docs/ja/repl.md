---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの挙動を素早く対話的にテストできる `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を求め、ターン間の会話履歴を保持します。デフォルトでは、生成されたモデル出力をそのままストリーミングします。上記の例を実行すると、`run_demo_loop` は対話型のチャットセッションを開始します。あなたの入力を連続して求め、ターン間の会話全体を記憶するため（エージェントが何について話したか把握できます）、生成と同時にエージェントの応答をリアルタイムで自動ストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` キーボードショートカットを使用します。