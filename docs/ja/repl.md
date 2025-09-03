---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナル上でエージェントの動作を素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成されると同時にモデル出力をストリーミングします。上記の例を実行すると、`run_demo_loop` は対話型チャットセッションを開始します。継続的に入力を求め、ターン間で会話全体の履歴を記憶するため（エージェントが何について話したかを把握できます）、生成と同時にリアルタイムでエージェントの応答を自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力（そして Enter キーを押す）するか、`Ctrl-D` のキーボードショートカットを使用します。