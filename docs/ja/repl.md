---
search:
  exclude: true
---
# REPL ユーティリティ

 SDK は `run_demo_loop` を提供しており、ターミナル内で エージェント の挙動を迅速かつインタラクティブにテストできます。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はユーザー入力を促すループを実行し、各ターン間の会話履歴を保持します。デフォルトでは、生成されたモデル出力を ストリーミング します。上記の例を実行すると、 run_demo_loop がインタラクティブなチャット セッションを開始します。ツールは継続的にあなたの入力を尋ね、各ターン間の完全な会話履歴を記憶するため、エージェント は何が議論されたかを把握できます。また、生成されると同時にリアルタイムで エージェント の応答を自動的に ストリーミング します。

チャット セッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボード ショートカットを使用してください。