---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここでは主に次の 2 種類のコンテキストがあります。

1. ローカルにコードから利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、 dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

** 最も重要 ** な注意点: 特定のエージェント実行において、そのエージェント、ツール関数、ライフサイクルなどはすべて同じ「型」のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行用の文脈データ（例: ユーザー名 / uid など、 ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得器など）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツール実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリックの `UserInfo` を指定します（例えば、異なるコンテキスト型を受け取るツールを渡した場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、参照できるのは会話履歴にあるデータ **のみ** です。つまり、LLM に新しいデータを利用可能にしたい場合は、そのデータが会話履歴から参照できるようにする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「 システムプロンプト 」または「developer message」とも呼ばれます。システムプロンプトは固定文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。 ユーザー 名や現在の日付のように常に役立つ情報に適した一般的な手法です。
2. `Runner.run` を呼ぶときの `input` に追加します。これは `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツール 経由で公開します。これはオンデマンドのコンテキストに適しており、LLM が必要に応じてツールを呼び出してデータを取得できます。
4. リトリーバル (retrieval) や Web 検索 を利用します。これらは、ファイルやデータベース（リトリーバル）または Web（ Web 検索 ）から関連データを取得できる特別なツールです。関連する文脈データに基づいて応答をグラウンディングするのに役立ちます。