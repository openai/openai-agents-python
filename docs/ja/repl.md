---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上でエージェントの動作を手早く対話的にテストできる `run_demo_loop` が用意されています。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成と同時にモデルの出力を ストリーミング します。上記の例を実行すると、`run_demo_loop` が対話的なチャットセッションを開始します。継続的に入力を求め、ターン間の会話全体の履歴を保持するため（あなたのエージェントが何が議論されたかを把握できます）、生成され次第、エージェントの応答をリアルタイムで自動 ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` のキーボードショートカットを使います。