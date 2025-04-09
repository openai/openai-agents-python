# エージェントの可視化

エージェントの可視化を使用すると、**Graphviz** を用いてエージェントとその関係性を構造化したグラフィカルな表現として生成できます。これにより、アプリケーション内でエージェント、ツール、およびハンドオフがどのように相互作用するかを理解できます。

## インストール

オプションの依存関係グループ `viz` をインストールします：

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用してエージェントの可視化を生成できます。この関数は以下のような有向グラフを作成します：

- **エージェント** は黄色の四角形で表されます。
- **ツール** は緑色の楕円形で表されます。
- **ハンドオフ** はあるエージェントから別のエージェントへの有向エッジで表されます。

### 使用例

```python
from agents import Agent, function_tool
from agents.extensions.visualization import draw_graph

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    tools=[get_weather],
)

draw_graph(triage_agent)
```

![Agent Graph](../assets/images/graph.png)

これにより、**triage agent** とそのサブエージェントおよびツールとの接続構造を視覚的に表現したグラフが生成されます。

## 可視化の理解

生成されたグラフには以下が含まれます：

- エントリーポイントを示す **開始ノード** (`__start__`)。
- 黄色で塗りつぶされた四角形で表されるエージェント。
- 緑色で塗りつぶされた楕円形で表されるツール。
- 相互作用を示す有向エッジ：
  - エージェント間のハンドオフは **実線矢印**。
  - ツールの呼び出しは **点線矢印**。
- 実行終了地点を示す **終了ノード** (`__end__`)。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは、`draw_graph` はグラフをインラインで表示します。グラフを別ウィンドウで表示するには、以下のように記述します：

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは、`draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します：

```python
draw_graph(triage_agent, filename="agent_graph.png")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。