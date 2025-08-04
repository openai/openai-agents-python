---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上でエージェントの挙動を素早く対話的にテストできる `run_demo_loop` が 用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループ内で ユーザー入力 を促し、ターン間で 会話履歴 を保持します。デフォルトでは、生成されたとおりにモデル出力を ストリーミング します。上記の例を実行すると、 `run_demo_loop` が 対話型チャットセッション を開始します。これはあなたの入力を継続的に求め、全 会話履歴 をターン間で記憶するため、エージェントはこれまでに話し合われた内容を把握できます。また、生成されたエージェントの応答を リアルタイム で自動的にストリーミング表示します。

チャットセッションを終了するには、`quit` または `exit` と入力（ Enter キーを押す）するか、 `Ctrl-D` キーボードショートカットを使用してください。