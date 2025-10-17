---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上で エージェント の挙動を手早く対話的にテストできる `run_demo_loop` が用意されています。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、各ターン間の会話履歴を保持します。デフォルトでは、生成と同時にモデル出力をストリーミングします。上の例を実行すると、 run_demo_loop は対話型チャットセッションを開始します。継続的に入力を尋ね、各ターン間でも会話全体の履歴を保持するため（エージェント が何を議論したか把握できます）、生成され次第リアルタイムで エージェント の応答を自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter を押す）、または `Ctrl-D` のキーボードショートカットを使用します。