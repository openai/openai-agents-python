---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。ここでは注目すべきコンテキストには主に次の 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック中、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はあなたのコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

 **最も重要** な注意点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行のためのコンテキストデータ（例: ユーザー名／ UID やその他の ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信される **ものではありません** 。完全にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型が使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っているのがわかります。ツールの実装はコンテキストから読み取ります。
3. ジェネリックな `UserInfo` でエージェントにマークを付け、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴のものだけです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で利用可能になるようにする必要があります。これにはいくつかの方法があります。

1. エージェントの `instructions` に追加します。これは「system prompt」または「開発者メッセージ」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。これは常に有用な情報（例: ユーザー名や現在の日付）に一般的な戦術です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` の戦術に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを持てる点が異なります。
3. 関数ツール 経由で公開します。これは  _オンデマンド_  のコンテキストに有用です。LLM が必要なときに判断し、そのデータを取得するためにツールを呼び出せます。
4. リトリーバル (retrieval) または Web 検索 を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。