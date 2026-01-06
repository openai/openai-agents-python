---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルで直接、エージェント の動作を素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、ターン間で会話の履歴を保持します。デフォルトでは、生成と同時にモデル出力を ストリーミング します。上記の例を実行すると、run_demo_loop は対話型のチャットセッションを開始します。継続的に入力を求め、ターン間で会話全体の履歴を記憶するため（エージェント が何を議論したかを把握できます）、生成されると同時に、エージェント の応答をリアルタイムで自動的に ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用します。