---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべき主なコンテキストは 2 つあります:

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作は次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的なパターンは、dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要な点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使わなければなりません。

コンテキストは次のような用途に使えます:

-   実行に関するコンテキストデータ（例: ユーザー名 / UID など、ユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得機構など）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。ローカルなオブジェクトであり、読み書きやメソッド呼び出しのみが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っているのが分かります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリクス `UserInfo` を指定します（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されると、参照できるのは会話履歴にあるデータのみです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツール経由で公開します。これはオンデマンドのコンテキストに有用で、LLM が必要に応じてツールを呼び出してデータを取得できます。
4. リトリーバルまたは Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）や Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答をグラウンディングするのに役立ちます。