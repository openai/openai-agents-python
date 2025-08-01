---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナル上でエージェントの挙動を素早くインタラクティブにテストできる `run_demo_loop` を提供しています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されたとおりにモデルの出力をストリーミングします。上記の例を実行すると、 run_demo_loop がインタラクティブなチャット セッションを開始します。このセッションではユーザー入力を継続的に受け取り、ターン間の全会話履歴を記憶するため、エージェントはすでに話題に上がった内容を理解できます。また、エージェントの応答を生成と同時にリアルタイムで自動ストリーミングします。

チャット セッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボード ショートカットを使用してください。