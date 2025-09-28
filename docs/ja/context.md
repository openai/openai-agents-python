---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。主に次の 2 つのコンテキストがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答を生成するときに LLM が参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

最も重要な注意点: あるエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行時のコンテキストデータ（例: ユーザー名/uid などの ユーザー 情報）
-   依存関係（例: ロガーオブジェクト、データ取得クラスなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは **not** LLM に送信されません。あくまでローカルのオブジェクトであり、読み書きやメソッド呼び出しができます。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取るのが分かります。ツール実装はコンテキストから読み取ります。
3. エージェントをジェネリック型 `UserInfo` で注釈し、型チェッカーがエラーを検出できるようにしています（たとえば、異なるコンテキスト型を受け取るツールを渡した場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されると、参照できるのは会話履歴のデータだけです。したがって、LLM に新しいデータを利用させたい場合は、その履歴で参照可能になるようにする必要があります。主な方法は次のとおりです。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した方法です。
2. `Runner.run` を呼び出すときに `input` に追加します。これは `instructions` の戦術に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に置かれるメッセージを扱えます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに有用です。LLM が必要に応じてデータ取得のタイミングを判断し、ツールを呼び出してデータを取得できます。
4. リトリーバル (retrieval) や Web 検索を使います。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータで応答をグラウンディングするのに有用です。