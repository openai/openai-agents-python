# **Output Types: Structured Output with Pydantic**
Structured outputs are crucial when you need your AI agents to provide data in a predictable and usable format. Instead of free-form text, you can enforce a specific structure, making it easier for your applications to parse and utilize the information. The OpenAI Agents SDK provides mechanisms to achieve this.

**Why Use Structured Outputs?**

**Data Parsing**: Easier to extract specific information without complex text parsing.

**Integration**: Seamless integration with other systems that expect structured data (e.g., databases, APIs).

**Reliability**: Reduces ambiguity and ensures consistent data formatting.

**Automation**: Automate workflows that rely on specific data points.


Using uv to run the project

    uv run chainlit run app.py -w
