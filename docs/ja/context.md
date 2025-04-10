# コンテキスト管理

コンテキストは多義的な用語です。考慮すべき主なコンテキストのクラスは2つあります。

1. コードにローカルで利用可能なコンテキスト: これは、ツール関数が実行されるときや、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、LLM が応答を生成するときに参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスとその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティを通じて表現されます。動作の仕組みは次の通りです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは、データクラスや Pydantic オブジェクトを使用することです。
2. そのオブジェクトを様々な実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどにラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` を通じてアクセスできます。

**最も重要な**点は、特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどが同じ _型_ のコンテキストを使用しなければならないことです。

コンテキストは以下のような用途に使用できます。

- 実行のためのコンテキストデータ（例: ユーザー名/uid やその他のユーザー情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは **LLM に送信されません**。これは純粋にローカルなオブジェクトであり、読み書きやメソッドの呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここではデータクラスを使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. エージェントをジェネリック `UserInfo` でマークし、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

## エージェント/LLM コンテキスト

LLM が呼び出されるとき、**唯一** 参照できるデータは会話履歴からのものです。新しいデータを LLM に利用可能にしたい場合、それを履歴に含める方法で行う必要があります。これにはいくつかの方法があります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。常に有用な情報（例: ユーザーの名前や現在の日付）に対して一般的な戦術です。
2. `Runner.run` 関数を呼び出す際に `input` に追加します。これは `instructions` 戦術に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを持つことができます。
3. 関数ツールを通じて公開します。これはオンデマンドのコンテキストに有用です。LLM がデータを必要とするときにツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらはファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータで応答を「グラウンド」するのに有用です。