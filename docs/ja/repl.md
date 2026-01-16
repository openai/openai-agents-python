---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は `run_demo_loop` を提供しており、ターミナル上で エージェント の挙動を素早く対話的にテストできます。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成されると同時にモデルの出力を ストリーミング します。上のサンプルを実行すると、`run_demo_loop` が対話型チャットセッションを開始します。継続的に入力を尋ね、ターン間の会話履歴全体を記憶するため（これにより エージェント は何が話されたかを把握します）、生成と同時に エージェント の応答をリアルタイムで自動 ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter キーを押す）、または `Ctrl-D` キーボードショートカットを使用してください。