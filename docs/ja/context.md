# コンテキスト管理

コンテキストという用語は多義的です。主に次の 2 種類のコンテキストがあります。

1. コード内でローカルに利用可能なコンテキスト：これは、関数ツールの実行時、`on_handoff` などのコールバック時、ライフサイクルフック時などに必要となるデータや依存関係です。
2. LLM が利用可能なコンテキスト：これは、LLM が応答を生成する際に参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティを通じて表現されます。動作の仕組みは以下の通りです。

1. 任意の Python オブジェクトを作成します。一般的にはデータクラスや Pydantic オブジェクトを使用します。
2. 作成したオブジェクトを各種の実行メソッドに渡します（例：`Runner.run(..., context=whatever)`）。
3. すべての関数ツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` を通じてアクセスできます。

**最も重要な点** は、特定のエージェント実行において、すべてのエージェント、関数ツール、ライフサイクルフックなどが同じコンテキストの _型_ を使用しなければならないということです。

コンテキストは以下のような用途で使用できます。

- 実行時のコンテキストデータ（ユーザー名や UID など、ユーザーに関する情報）
- 依存関係（ロガーオブジェクト、データ取得用オブジェクトなど）
- ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM には送信され **ません**。これは純粋にローカルなオブジェクトであり、読み取り、書き込み、メソッド呼び出しが可能です。

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
    return f"User {wrapper.context.name} is 47 years old"

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

1. これがコンテキストオブジェクトです。ここではデータクラスを使用していますが、任意の型を使用できます。
2. これは関数ツールです。`RunContextWrapper[UserInfo]` を引数として受け取り、コンテキストからデータを読み取っています。
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーがエラーを検出できるようにしています（例えば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. コンテキストを `run` 関数に渡しています。
5. エージェントが正しくツールを呼び出し、年齢を取得しています。

## エージェント / LLM コンテキスト

LLM が呼び出される際、参照できるデータは会話履歴に含まれるもの **のみ** です。そのため、新しいデータを LLM に提供したい場合は、会話履歴に含まれるようにする必要があります。これを実現する方法はいくつかあります。

1. エージェントの `instructions` に追加する方法です。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でもよく、またはコンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（ユーザー名や現在の日付など）を提供する際によく使われる手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加する方法です。これは `instructions` と似ていますが、[指揮系統（chain of command）](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に位置するメッセージを設定できます。
3. 関数ツールを通じて公開する方法です。これは _オンデマンド_ なコンテキストに有効で、LLM が必要なデータを判断し、そのデータを取得するためにツールを呼び出します。
4. 検索（retrieval）やウェブ検索を使用する方法です。これらはファイルやデータベースから関連データを取得（検索）したり、ウェブからデータを取得（ウェブ検索）したりする特殊なツールです。これは関連するコンテキストデータに基づいて応答を「根拠付ける（grounding）」際に有効です。