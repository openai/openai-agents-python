---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。意識すべき主なコンテキストには次の 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: これはツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性のあるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは LLM が応答を生成する際に目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどにはラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` でアクセスできます。

最も重要な点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使わなければなりません。

コンテキストは次のような用途に使えます:

-   実行のための状況データ（例: ユーザー名 / uid や、ユーザーに関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツールの実装はコンテキストから読み取っています。
3. 型チェッカーがエラーを検出できるように（たとえば異なるコンテキスト型を受け取るツールを渡そうとした場合など）、エージェントにジェネリックの `UserInfo` を付与しています。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されると、LLM が参照できるデータは会話履歴からのものだけです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴で利用可能になる方法で行う必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「system prompt」や「デベロッパーメッセージ」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例: ユーザー名や現在の日付）に一般的な戦略です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の戦略に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツールを通じて公開します。これは  _オンデマンド_  のコンテキストに有用です — LLM が必要に応じて判断し、データを取得するためにツールを呼び出せます。
4. リトリーバル（retrieval）や Web 検索を使用します。これらは、ファイルやデータベース（retrieval）から、あるいは Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連する状況データに応答を「グラウンディング」するのに役立ちます。