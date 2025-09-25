---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルで直接 エージェント の挙動をすばやく対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、各ターン間で会話履歴を保持します。デフォルトでは、生成され次第、モデルの出力を ストリーミング します。上の例を実行すると、run_demo_loop は対話型のチャットセッションを開始します。あなたの入力を継続的に求め、各ターン間で会話全体の履歴を記憶し（これにより エージェント は何が話されたかを把握できます）、生成と同時に エージェント の応答をリアルタイムで自動 ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter キーを押す）、または `Ctrl-D` のキーボードショートカットを使用します。