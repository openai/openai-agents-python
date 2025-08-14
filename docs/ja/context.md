---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここで重要になるコンテキストは大きく 2 つに分けられます:

1. あなたのコードでローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック内、ライフサイクルフック内などで必要になるデータや依存関係です。
2. LLM から利用できるコンテキスト: 応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的なパターンは、 dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種の実行メソッドに渡します (例えば `Runner.run(..., **context=whatever**))`)。
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はあなたのコンテキストオブジェクトの型を表し、`wrapper.context` から参照できます。

最も注意すべき **最も重要** な点: 特定のエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ _型_ のコンテキストを使わなければなりません。

コンテキストは次の用途に使えます:

-   実行用のコンテキストデータ (例えば、ユーザー名 / uid やユーザーに関するその他の情報)
-   依存関係 (例えば、ロガーのオブジェクト、データ取得処理など)
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。あくまでローカルなオブジェクトであり、読み取り・書き込みやメソッド呼び出しができます。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることが分かります。ツールの実装はコンテキストから読み取ります。
3. エージェントにジェネリックな `UserInfo` を付けることで、型チェッカーが誤りを検出できます (例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など)。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM を呼び出したとき、LLM が見られるのは会話履歴にあるデータだけです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴に現れる形で提供しなければなりません。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「システムプロンプト」や「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列にも、コンテキストを受け取って文字列を出力する動的な関数にもできます。常に有用な情報 (例えばユーザー名や現在の日付) に適した方法です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` を使う方法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上でより下位のメッセージとして配置できます。
3. 関数ツールとして公開します。これは _オンデマンド_ コンテキストに有用です。LLM が必要なときにデータを判断し、そのデータを取得するためにツールを呼び出せます。
4. リトリーバルや Web 検索を使います。これらは、ファイルやデータベース (リトリーバル) から、または Web (Web 検索) から関連データを取得できる特別なツールです。応答を関連するコンテキストデータで「根拠付け (grounding)」するのに役立ちます。