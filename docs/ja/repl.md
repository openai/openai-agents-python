---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナル上でエージェント の振る舞いを素早く対話的にテストできる `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループでユーザー入力を促し、ターン間で会話履歴を保持します。既定では、生成と同時にモデル出力をストリーミングします。上の例を実行すると、`run_demo_loop` が対話的なチャットセッションを開始します。ユーザー入力を継続的に求め、ターン間の会話履歴全体を保持します（そのため、エージェント が何について話したかを把握できます）。また、エージェント の応答を生成と同時にリアルタイムで自動ストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（ Enter を押す）、または `Ctrl-D` のキーボードショートカットを使用します。