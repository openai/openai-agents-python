---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべきコンテキストには主に 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM から利用できるコンテキスト: これは、LLM が応答を生成する際に参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も  **重要**  な点: 特定のエージェント実行で関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ  _型_  のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行用のコンテキストデータ（例: ユーザー名 / uid やその他のユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得クラスなど）
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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装ではコンテキストから読み取っています。
3. 型チェッカーでエラーを検出できるように、エージェントにジェネリック `UserInfo` を付けています（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されると、LLM が参照できるデータは会話履歴からのもの  **のみ**  です。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを指定できます。
3. 関数ツールで公開します。これは  _オンデマンド_  のコンテキストに役立ちます。LLM が必要に応じて判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバル（retrieval）や Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、応答を関連するコンテキストデータに「グラウンディング」するのに役立ちます。