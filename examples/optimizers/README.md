# Optimizers Examples

Standalone examples demonstrating the optimization utilities. Includes real-world email triage scenarios and structured output demos.

Run them with:

```bash
# Real-world style (email triage)
uv run python examples/optimizers/email_triage_greedy.py
uv run python examples/optimizers/email_triage_random.py
uv run python examples/optimizers/email_triage_instructions.py

# Structured output (CalendarEvent)
uv run python examples/optimizers/calendar_events_greedy.py
uv run python examples/optimizers/calendar_events_random.py
uv run python examples/optimizers/calendar_events_instructions.py
```

The email triage examples use your default model settings (e.g., OpenAI). Set `OPENAI_API_KEY` to run them. Structured output examples also work with OpenAI Responses and `output_type`.
