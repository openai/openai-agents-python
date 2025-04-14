# コンテキスト管理

コンテキストは多義的な用語です。主に関心を持つべきコンテキストには、次の 2 つの大きなクラスがあります。

1. コード内でローカルに利用可能なコンテキスト：これは、ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用可能なコンテキスト：これは、LLM がレスポンスを生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティによって表現されます。仕組みは以下の通りです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッド（例：`Runner.run(..., **context=whatever**))`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

**最も重要**な注意点：特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは、同じ _型_ のコンテキストを使用する必要があります。

コンテキストは以下のような用途で利用できます。

-   実行時のコンテキストデータ（例：ユーザー名/uid やユーザーに関するその他の情報など）
-   依存関係（例：ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは **LLM には送信されません**。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を利用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることが分かります。ツールの実装はコンテキストから値を読み取ります。
3. エージェントにはジェネリック型 `UserInfo` を指定しています。これにより、型チェッカーがエラーを検出できます（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

## エージェント／LLM コンテキスト

LLM が呼び出される際、**唯一** 参照できるデータは会話履歴からのものです。つまり、LLM に新しいデータを利用させたい場合は、そのデータを履歴に含める必要があります。これを実現する方法はいくつかあります。

1. エージェントの `instructions` に追加する。この方法は「システムプロンプト」や「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。たとえば、ユーザー名や現在の日付など、常に有用な情報に適しています。
2. `Runner.run` 関数を呼び出す際に `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位メッセージとして追加できます。
3. 関数ツールを通じて公開する。この方法は _オンデマンド_ のコンテキストに適しています。LLM が必要なタイミングでツールを呼び出し、データを取得できます。
4. リトリーバルや Web 検索を利用する。これらはファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいたレスポンスを「グラウンディング」するのに役立ちます。