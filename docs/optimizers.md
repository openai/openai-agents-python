### Optimizers and Evaluation

The Agents SDK includes an optimization module inspired by DSPy that helps you evaluate and improve agents. It supports both text and structured outputs (via `output_type`).

#### Features

- `evaluate_agent(agent, dataset, metric)`: Evaluate an agent over labeled examples. Metrics can be synchronous or asynchronous (e.g., LLM-as-judge).
- `cross_validate_agent(agent, dataset, k_folds=5)`: K-fold cross validation.
- `BootstrapFewShot(max_examples=4, base_instructions=None)`: Greedy few-shot selection, automatically injected via `RunConfig.call_model_input_filter`.
- `BootstrapFewShotRandomSearch(max_examples=4, num_trials=32, seed=None)`: Random-search few-shot selection.
- `InstructionOptimizer(candidates=[...])`: Searches over system instruction candidates.

All optimizers return an `OptimizerResult` with:

- `call_model_input_filter`: A filter that augments model inputs (few-shot, etc.).
- `updated_instructions`: Optional system prompt to apply.
- `selected_examples`: The demonstrations chosen (if any).
- `score`: The validation score achieved during optimization.

Use `OptimizerResult.attach_to_runconfig()` to compose the learned augmentation with a `RunConfig` for future runs.

#### Structured output support

- Few-shot examples automatically serialize structured expected outputs to JSON (Pydantic `BaseModel`, `dict`, `list`), so the model sees target shapes.
- Evaluation prefers the structured `final_output` from the agent when computing metrics, and falls back to concatenated text only if needed.

#### Quick start (text)

```python
from agents import Agent, LabeledExample, evaluate_agent, exact_match_metric, BootstrapFewShot

agent = Agent(name="Assistant", instructions="Answer concisely")
dataset = [
    LabeledExample(input="A?", expected="1"),
    LabeledExample(input="B?", expected="2"),
]

baseline = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric)

opt = BootstrapFewShot(max_examples=2)
res = await opt.fit(agent, dataset)
cfg = res.attach_to_runconfig()

improved = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric, run_config=cfg)
```

#### Structured output example (CalendarEvent)

```python
from pydantic import BaseModel
from agents import Agent, LabeledExample, evaluate_agent, BootstrapFewShot

class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

def event_metric(pred, exp) -> float:
    p = pred if isinstance(pred, dict) else pred.model_dump()
    e = exp if isinstance(exp, dict) else exp.model_dump()
    score = 0.0
    score += 1.0 if p.get("name") == e.get("name") else 0.0
    score += 1.0 if p.get("date") == e.get("date") else 0.0
    score += 1.0 if list(p.get("participants", [])) == list(e.get("participants", [])) else 0.0
    return score / 3.0

agent = Agent(
    name="Calendar extractor",
    instructions="Extract CalendarEvent JSON",
    output_type=CalendarEvent,
)

dataset = [
    LabeledExample(
        input="Team sync on 2025-10-03 10:00 with Bob",
        expected=CalendarEvent(name="Team sync", date="2025-10-03 10:00", participants=["Bob"]),
    )
]

baseline = await evaluate_agent(agent, dataset=dataset, metric=event_metric)

opt = BootstrapFewShot(max_examples=1)
res = await opt.fit(agent, dataset)
cfg = res.attach_to_runconfig()

improved = await evaluate_agent(agent, dataset=dataset, metric=event_metric, run_config=cfg)
```

#### Real-world example: email triage

See `examples/optimizers/email_triage_*.py` for greedy few-shot, random search, and instruction search. These use a tolerant text metric that checks whether the predicted label appears in the response.

#### API reference

- `agents.optimizers.evaluate_agent`
- `agents.optimizers.cross_validate_agent`
- `agents.optimizers.BootstrapFewShot`
- `agents.optimizers.BootstrapFewShotRandomSearch`
- `agents.optimizers.InstructionOptimizer`
- `agents.optimizers.OptimizerResult`


