---
search:
  exclude: true
---
# エージェントの可視化

エージェントの可視化を利用すると、 **Graphviz** を使ってエージェントとそれらの関係を構造化したグラフィカル表現として生成できます。これにより、アプリケーション内でエージェント、ツール、ハンドオフがどのように相互作用しているかを理解しやすくなります。

## インストール

オプションの `viz` 依存関係グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用してエージェントの可視化を生成できます。この関数は次のような有向グラフを作成します:

- **エージェント** は黄色いボックスで表されます。
- **ツール** は緑色の楕円で表されます。
- **ハンドオフ** はあるエージェントから別のエージェントへ向かう有向エッジとして表されます。

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

これにより、 **triage agent** の構造とサブエージェントおよびツールとの接続を視覚的に表したグラフが生成されます。

## 可視化の理解

生成されたグラフには次が含まれます:

- エントリポイントを示す **start node** (`__start__`)。
- 黄色で塗りつぶされた **長方形** で表されるエージェント。
- 緑色で塗りつぶされた **楕円** で表されるツール。
- 相互作用を示す有向エッジ:
  - エージェント間のハンドオフには **実線の矢印**。
  - ツール呼び出しには **点線の矢印**。
- 実行が終了する場所を示す **end node** (`__end__`)。

## グラフのカスタマイズ

### グラフの表示
既定では `draw_graph` はグラフをインラインで表示します。別ウィンドウでグラフを表示するには次のように記述します:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
既定では `draw_graph` はグラフをインラインで表示します。ファイルとして保存するにはファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。