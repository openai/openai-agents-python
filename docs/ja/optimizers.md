### 最適化と評価

Agents SDK には DSPy に着想を得た最適化モジュールが含まれており、エージェントを評価・改善できます。`output_type` による構造化出力にも対応しています。

#### 機能

- `evaluate_agent(agent, dataset, metric)`: ラベル付きデータで評価（同期/非同期メトリクス対応）。
- `cross_validate_agent(agent, dataset, k_folds=5)`: k 分割交差検証。
- `BootstrapFewShot(max_examples=4)`: 貪欲法で few-shot 例を選択し、`RunConfig.call_model_input_filter` で入力に注入。
- `BootstrapFewShotRandomSearch(max_examples=4, num_trials=32)`: ランダム探索による few-shot 選択。
- `InstructionOptimizer(candidates=[...])`: システムプロンプト候補を探索。

構造化出力の few-shot 例は自動的に JSON 化され、評価時は構造化された `final_output` が優先されます。

#### 構造化出力例（CalendarEvent）

```python
from pydantic import BaseModel
from agents import Agent, LabeledExample, evaluate_agent, BootstrapFewShot

class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(name="Calendar extractor", instructions="Extract CalendarEvent JSON", output_type=CalendarEvent)
dataset = [LabeledExample(input="Team sync on 2025-10-03 10:00 with Bob", expected=CalendarEvent(name="Team sync", date="2025-10-03 10:00", participants=["Bob"]))]
```

詳細なコード例は `examples/optimizers/` を参照してください。


