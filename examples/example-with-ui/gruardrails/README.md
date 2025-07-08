# **Guardrails**
Summary: OpenAI have also included guardrails in the Agents SDK. These come as input guardrails and output guardrails, the input_guardrail checks that the input going into your LLM is "safe" and the output_guardrail checks that the output from your LLM is "safe".

Guardrails run in parallel to your agents, enabling you to do checks and validations of user input. For example, imagine you have an agent that uses a very smart (and hence slow/expensive) model to help with customer requests. You wouldn't want malicious users to ask the model to help them with their math homework. So, you can run a guardrail with a fast/cheap model. If the guardrail detects malicious usage, it can immediately raise an error, which stops the expensive model from running and saves you time/money.

There are two kinds of guardrails:

1. Input guardrails run on the initial user input
2. Output guardrails run on the final agent output

Reference:
https://openai.github.io/openai-agents-python/guardrails/

Using uv to run the project

    uv run chainlit run app.py -w

