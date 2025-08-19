---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェント の挙動を素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、各ターン間の会話履歴を保持します。デフォルトでは、モデルの出力を生成され次第ストリーミングします。上の例を実行すると、 run_demo_loop が対話型チャットセッションを開始します。継続的に入力を尋ね、各ターン間の会話履歴全体を記憶し（エージェント が何が議論されたかを把握できるように）、生成されるそばからエージェント の応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用してください。