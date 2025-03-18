# Context
Check out the OpenAI Agents Python context documentation:
https://openai.github.io/openai-agents-python/context/

Context in OpenAI Agents Python allows you to provide additional information to the assistant during conversations. This can include relevant documentation, code snippets, or any other text that helps the assistant better understand the task at hand. Context is passed as a list of strings and can be updated dynamically as the conversation progresses.
 
`uv add openai-agents python-dotenv chainlit`

Give these commands to run the project:

    cd chatbot

    uv venv

On Mac:

    source .venv/bin/activate

Using uv to run the project

    uv run chainlit run app.py -w