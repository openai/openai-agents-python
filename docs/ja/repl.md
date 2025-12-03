---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルでエージェントの挙動を素早く対話的にテストできる `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。既定では、生成され次第モデル出力をストリーミングします。上のサンプルコードを実行すると、`run_demo_loop` はインタラクティブなチャットセッションを開始します。あなたの入力を継続的に求め、ターン間の会話全体を記憶するため（エージェントが何について話したか把握できます）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` のキーボードショートカットを使用します。