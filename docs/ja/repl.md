---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの動作を手早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループでユーザー入力を求め、ターン間の会話履歴を保持します。既定では、生成と同時にモデル出力をストリーミングします。上の例を実行すると、 run_demo_loop が対話的なチャットセッションを開始します。継続的に入力を求め、ターン間の会話全体の履歴を記憶します（エージェントが何が議論されたかを把握できるように）。また、生成され次第、エージェントの応答をリアルタイムで自動ストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力し（そして Enter を押す）、または `Ctrl-D` キーボードショートカットを使用してください。