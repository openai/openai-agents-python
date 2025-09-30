---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。ここでは主に次の 2 つのクラスのコンテキストがあります。

1. コードからローカルに利用可能なコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック中、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、LLM が応答を生成する際に参照するデータです。

## ローカルコンテキスト

これは、[`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティによって表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

 **最も重要な** 点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ _type_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行のためのコンテキストデータ（例: ユーザー名/uid など、ユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトであり、読み取り・書き込み・メソッド呼び出しができます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. エージェントにジェネリクスの `UserInfo` を指定し、型チェッカーがエラーを検知できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント/ LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴にあるもの **のみ** です。つまり、LLM に新しいデータを利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。これにはいくつかの方法があります:

1. Agent の `instructions` に追加します。これは "system prompt" または "developer message" とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に対して一般的な手法です。
2. `Runner.run` を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージにできます。
3. 関数ツールとして公開します。これはオンデマンドのコンテキストに有用で、LLM が必要なときにデータ取得のためツールを呼び出せます。
4. リトリーバルや Web 検索を使用します。これらはファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。