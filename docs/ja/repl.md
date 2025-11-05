---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの振る舞いを素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成され次第モデル出力を ストリーミング します。上記のサンプルを実行すると、run_demo_loop は対話的なチャットセッションを開始します。継続的に入力を求め、ターン間の会話全体を記憶するため（エージェントが何を議論したか把握できます）、生成されるそばからエージェントの応答を リアルタイム に自動的に ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用します。