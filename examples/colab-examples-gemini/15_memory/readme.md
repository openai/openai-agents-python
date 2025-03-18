# Agentic Long Term Memory in OpenAI Agentic 

[Different Types of Memories in Agentic Framework](https://www.linkedin.com/pulse/different-types-memories-agentic-framework-gourav-g--shdxc/)((

[Core Concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)

We will use this course to learn concepts: [Course: Long-Term Agentic Memory with LangGraph](https://www.deeplearning.ai/short-courses/long-term-agentic-memory-with-langgraph/)


Let's clarify and refine the understanding of memory types within the context of agentic frameworks, specifically with an eye towards how they might be implemented using tools like the OpenAI Agents SDK.

Here's a breakdown of the memory types and how they relate to agentic workflows:

**1. Memory Types in Agentic Frameworks:**

* **Episodic Memory:**
    * These are experiences that can help agents remember how to do tasks.
    * Stores individual interaction “episodes” or past events.
    * This stores specific, personal experiences or "episodes" of the agent's interaction with the environment or users.
    * Think of it as a log of what the agent has done and observed.
    * In an OpenAI Agents SDK context, this could involve storing:
        * **User inputs and agent responses.**
        * API call results.
        * Observations from tools the agent used.
        * Timestamps associated with each event.
    * Example: "At 2:30 PM, the user asked for the weather in London, and I called the weather API, which returned 15 degrees Celsius."

* **Semantic Memory:**
    * Stores facts, **user profiles**, and external knowledge.
    * This stores general knowledge and facts about the world.
    * It's about understanding concepts, relationships, and meanings.
    * In the Agents SDK, this could be:
        * Pre-trained knowledge embedded in the language model.
        * Knowledge retrieved from external knowledge bases (e.g., databases, APIs).
        * Information extracted and stored from previous interactions.
    * Example: "London is the capital of the United Kingdom." or "The capital of France is Paris."
    * Example: "Important Birthdates for Calender Agent"

* **Procedural Memory:**
    * Rules for Agents to follow.
    * Holds the agent’s “how-to” information, such as system instructions and operational rules.
    * This stores knowledge about how to do things, or "procedures."
    * It's about skills, habits, and learned behaviors.
    * It's is used to update your system prompt dynamically.
    * In the Agents SDK, this could involve:
        * Storing sequences of actions or tool calls that have been successful in the past.
        * Learning and refining strategies for achieving specific goals.
        * Storing the result of function calls, and the proper way to call those functions.
    * Example: "To get the weather, first, call the weather API with the city name, then parse the temperature from the response."

* **Temporal Memory:**
    * Captures the order and timing of events or interactions.
    * This is related to the agents ability to track and understand the passage of time, and the order of events.
    * This is often tightly coupled with episodic memory, but can also be considered it's own type.
    * It can be updated using a dynamic, temporally-aware knowledge graph that continuously integrates new information while tracking how relationships and facts evolve over time.
    * Bi-Temporal Modeling: Instead of a single timeline, the system employs dual time-tracking. One timeline captures the actual occurrence of events (when facts were valid), while a second timeline records the order in which data was ingested. This dual approach allows the memory layer to mark outdated information as invalid when new, conflicting data is received.
    * Dynamic Knowledge Integration: As new interactions occur or business data changes, the system extracts facts and relationships from both unstructured and structured sources. These facts are enriched with temporal metadata—such as timestamps indicating when a fact became valid and when it was superseded—ensuring that only current and relevant information is used.
    * Hybrid Retrieval: For efficient recall, the memory layer combines semantic similarity search, full-text retrieval, and graph-based queries. This hybrid approach ensures that the most pertinent, time-sensitive context is retrieved quickly, while also preserving the underlying relationships between entities.
    * In the Agents SDK, this would include.
        * Timestamps on all events.
        * The ability to understand "before" and "after" relationships.
        * The ability to understand time based context for actions.
    

**2. Memory Mechanisms:**

* **Hot Path (Immediate/Short-Term) Memory:**
    * This refers to the agent's immediate working memory.
    * It's used for processing the current task and keeping track of recent interactions.
    * In the Agents SDK, this would involve:
        * The context window of the language model.
        * Variables and data structures used during the execution of a function call.
        * The current conversation history.
    * This is the memory that the agent uses to create it's next response.

* **Background (Long-Term) Memory:**
    * This refers to the agent's persistent memory, which is stored and retrieved over longer periods.
    * In the Agents SDK, this would involve:
        * External databases or vector stores.
        * File storage.
        * Knowledge graphs.
    * This is the memory that the agent uses to inform it's actions over multiple conversations.
    * Memory updates are performed asynchronously. A separate process gathers and organizes new information after the primary response is generated, which decouples memory maintenance from the immediate conversation flow and reduces latency.

**3. You can:**

* **Configure Memory Stores:**
Set up persistent memory stores (such as vector databases, graph databases or JSON-based stores) for each type of memory. Semantic and episodic memories are often stored for retrieval during future interactions, while procedural memory is used to update your system prompt dynamically.

* **Customize Update Strategies:**
Choose between hot path updates for immediate context enrichment and background processes for less time-critical memory updates. This flexibility lets you balance performance and context preservation based on your application’s needs.

* **Leverage Memory in Prompts:**
When constructing your system prompt for an agent call, you can inject relevant memories—whether they are high-level summaries (from episodic memory) or key facts (from semantic memory)—to make responses more context-aware and personalized. Temporal memory helps ensure that the most recent interactions carry appropriate weight.

By combining these different memory types and update mechanisms, your agent becomes better equipped to maintain a coherent, contextually rich conversation over long interactions, adapt its behavior based on past events, and improve over time.

**This layered memory design is one of the key innovations in building robust agentic systems with the OpenAI Agents SDK. It helps transform a stateless model into one that is dynamically adaptive and contextually aware, closely mimicking how human memory contributes to intelligent behavior.**

**Applying this to OpenAI Agents SDK:**

* The Agents SDK provides the tools to build agents that can interact with external resources.
* You can use these tools to implement the different memory types.
* For example:
    * You could use a vector database (like Pinecone or Weaviate) to store episodic and semantic memory.
    * You could use function calling to implement procedural memory, by storing the results of function calls, and the proper way to call those functions.
    * You can use the conversation history that is managed by the SDK as the hot path memory.
* The choice of memory implementation will depend on the specific needs of your agent.

By understanding these memory types and mechanisms, you can design more sophisticated and capable agents that can learn, adapt, and reason effectively.

# **LangMem: Long-Term Memory Solutions for Intelligent Agents and Integration with the OpenAI Agents SDK**

You can use its core API with any storage system and within any Agent framework i.e. OpenAI Agents SDK

[Announcement](https://blog.langchain.dev/langmem-sdk-launch/)

[Long-term Memory: LangMem SDK Conceptual Guide](https://www.youtube.com/watch?v=snZI5ojuMRc)

[Course: Long-Term Agentic Memory with LangGraph](https://www.deeplearning.ai/short-courses/long-term-agentic-memory-with-langgraph/)


## **Introduction**

The development of sophisticated AI agents capable of engaging in coherent, personalized, and context-aware interactions necessitates the integration of robust long-term memory capabilities. Traditional large language model (LLM)-based agents often face limitations in retaining information and context across multiple sessions or lengthy conversations. LangMem, a software development kit (SDK) from LangChain, is specifically designed to address this challenge by providing AI agents with the ability to learn and adapt from their interactions over time. This report will delve into the definition and key features of LangMem, explore its potential for use with the OpenAI Agents SDK, and examine alternative solutions for implementing long-term memory in AI agents.

## **Understanding LangMem: Enabling Persistent Knowledge for AI Agents**

LangMem is engineered to equip AI agents with long-term memory, allowing them to store important details from conversations, refine their behavior based on experience, and maintain knowledge across different sessions 1. This capability moves agents beyond being merely reactive systems to becoming dynamic and adaptive assistants that retain context and improve with continued use 2. At its core, LangMem operates through a consistent pattern: it receives conversations and the current memory state, prompts an LLM to determine how to expand or consolidate the memory, and then updates the memory state accordingly 3.

Several key features distinguish LangMem. Its **storage-agnostic memory API** allows developers to utilize various storage systems, including in-memory storage and databases, providing flexibility in choosing the most suitable backend for their needs 2. LangMem also offers **active "hot-path" memory tools**, enabling agents to record and search information during live conversations, facilitating on-the-fly retrieval of relevant details 2. Furthermore, a **background memory manager** automatically extracts, consolidates, and updates the agent's knowledge outside the immediate conversation flow, ensuring continuous learning and improvement 2. Notably, LangMem features **native integration with LangGraph's long-term memory store**, making persistent memory readily available for agents built on the LangGraph platform 2.

LangMem categorizes long-term memory into three primary types: **semantic memory** for facts and knowledge, **episodic memory** for past experiences, and **procedural memory** for system instructions and learned behaviors 3. This categorization allows for a more structured approach to managing the different kinds of information an agent needs to retain. The SDK also supports both **conscious formation** (real-time memory updates during conversations) and **subconscious formation** (asynchronous background memory processing) of memories, catering to different application requirements 3. Memory operations within LangMem are organized into a **Core API**, which provides stateless functions for transforming memory states, and **Stateful Integration**, which leverages LangGraph's storage to persist and manage memories 3. Memories are further organized into **namespaces**, allowing for logical segmentation of data based on users, contexts, or other hierarchical structures, which is crucial for maintaining data privacy and preventing cross-contamination in multi-user applications 2.

## **Utilizing LangMem with the OpenAI Agents SDK**

The OpenAI Agents SDK provides a framework for building agentic AI applications by defining agents as LLMs configured with instructions, tools, handoffs, and guardrails 6. Tools are external functions that agents can call to perform specific tasks, extending their capabilities beyond the inherent knowledge of the LLM 6. The documentation for LangMem explicitly states its core API can be used with any storage system and within any Agent framework 12. This inherent flexibility allows for the integration of LangMem's long-term memory capabilities into agents built using the OpenAI Agents SDK.

Several approaches can be employed to achieve this integration. One method involves **wrapping LangMem's core API functions as tools** within the OpenAI Agents SDK 2. This allows the agent to utilize LangMem's memory management functionalities, such as storing and searching for information, as part of its available tools. For instance, create\_manage\_memory\_tool and create\_search\_memory\_tool from LangMem can be defined as tools within the OpenAI Agent, enabling the agent to decide autonomously when to save or retrieve information based on the conversation context 2.


By encapsulating LangMem's core API within OpenAI Agent SDK tools, developers can seamlessly incorporate long-term memory features into their agents. The agent then has the autonomy to determine when and how to leverage these memory tools depending on the specific requirements of the interaction.

Another crucial aspect of integrating LangMem involves **utilizing a suitable storage system** 2. While LangMem's core API is storage-agnostic, for true long-term memory, a persistent storage backend is necessary. InMemoryStore is provided by LangGraph and can be useful for testing purposes, but its contents are lost when the program restarts 2. For production deployments using the OpenAI Agents SDK with LangMem, it is advisable to opt for persistent storage solutions like AsyncPostgresStore or other database integrations offered by LangChain to ensure that the agent's memories are retained across sessions 2. The selection of the storage backend directly influences the durability and scalability of the agent's long-term memory. Production environments necessitate persistent storage to maintain knowledge over time.

Furthermore, **namespacing memories** is essential for managing context, especially in applications involving multiple users or distinct contexts within the OpenAI Agents SDK and LangMem 2. Namespaces allow for the isolation of memory entries, preventing data from different users or contexts from interfering with each other 2. When creating LangMem memory tools for use with the OpenAI Agents SDK, namespaces can be incorporated to ensure proper contextual isolation.


In scenarios where multiple users interact with the same agent built with the OpenAI Agents SDK, employing namespaces within LangMem is critical for preserving data privacy and ensuring that each user's interactions and preferences are stored and retrieved independently.

## **Benefits and Use Cases of Combining LangMem and OpenAI Agents SDK**

The integration of LangMem with the OpenAI Agents SDK offers several significant advantages. Agents can achieve **enhanced personalization** by remembering user preferences and past interactions, leading to more tailored and relevant responses 2. This combination also fosters **improved contextual understanding**, as agents can leverage long-term memory to maintain context across extended conversations and multiple sessions, avoiding the stateless nature of traditional LLM interactions 2. Furthermore, agents can exhibit **dynamic learning and adaptation**, continuously refining their behavior and knowledge based on their accumulated experiences 1. This enables the creation of truly **stateful applications** that retain information and evolve in response to user interactions 2. Practical use cases for such integrated systems include customer support agents that recall past issues to provide more efficient assistance, personal assistants that track user schedules and preferences over time, and educational tools that adapt to a student's learning history.

## **Exploring Alternatives to LangMem for AI Agent Memory**

While LangMem provides a robust solution for long-term memory in AI agents, several alternative frameworks and approaches exist in the rapidly evolving landscape of AI. These alternatives may offer different architectural designs, feature sets, or integration capabilities that might be more suitable for specific use cases.

### **MemGPT**

MemGPT is an innovative framework inspired by operating system architectures, specifically designed to enable LLMs to manage their own memory and overcome the limitations of restricted context windows 15. The core concept behind MemGPT involves a memory hierarchy, comprising in-context (core) memory and out-of-context (archival) memory, managed by the agent itself through tool calls 17. This architecture allows agents to handle a larger virtual context than the native context window of the LLM, enabling them to engage in more extensive and complex interactions 15. Key features of MemGPT include **self-editing memory**, where the agent can update its own persona and user information, **tool utilization** for interacting with external resources like web search or APIs, and **integration with external data sources** such as filesystems and databases 15. Notably, MemGPT has demonstrated seamless integration with agent frameworks like AutoGen, allowing AutoGen agents to leverage its advanced memory management capabilities 16. MemGPT's approach directly addresses the context window limitation inherent in many LLMs by providing a mechanism for managing a more expansive virtual context, thereby enabling agents to process and retain more information over longer interactions.

### **Zep**

Zep presents itself as a long-term memory service for agentic applications, utilizing a **temporal knowledge graph** to continuously learn from user interactions and evolving business data 23. Zep autonomously constructs a knowledge graph for each user, capturing entities, relationships, and facts, and importantly, tracking how these evolve over time 24. This knowledge graph approach enables agents to reason about the relationships between different pieces of information and understand the temporal context of changes 24. Key features of Zep include the ability to **capture detailed conversational context**, integrate **business data from various sources**, implement **fact ratings** for quality control of retrieved information, and provide **efficient memory retrieval** with low latency 24. Zep has shown strong performance in benchmarks like LongMemEval, highlighting its effectiveness in handling complex, long-term memory tasks 28. It also offers integrations with popular agent frameworks such as LangChain and AutoGen, with SDKs available in Python, TypeScript, and Go, facilitating its adoption in diverse development environments 26. Representing memory as a graph allows for more intricate queries and a deeper understanding of the connections between various pieces of information.

### **Other Notable Memory Management Solutions**

Beyond LangMem, MemGPT, and Zep, several other solutions cater to the need for memory management in AI agents. **LangChain itself provides a variety of built-in memory types**, including ConversationBufferMemory, ConversationBufferWindowMemory, ConversationSummaryMemory, ConversationSummaryBufferMemory, and memory backed by vector stores 32. These options offer different strategies for retaining and retrieving conversational history, ranging from simply storing all messages to summarizing past interactions to manage token limits 32. For developers already working within the LangChain ecosystem, these built-in memory options provide a convenient and readily available starting point for adding conversational memory to their agents.

**Memoripy** is a newer project that supports both Ollama and OpenAI, with a focus on modeling memory in a way that prioritizes more important memories, potentially leading to more efficient memory utilization 37. **Mem0** is described as an "intelligent memory layer" that offers flexibility by allowing the use of various LLMs through LiteLLM. Other projects mentioned in the community include Letta (formerly the open-source MemGPT project), cognee, Haystack Basic Agent Memory Tool, memary, kernel-memory, LangGraph Memory Service, txtai, Langroid, WilmerAI, and EMENT, each with its own unique features and focuses 17.

## **Comparative Analysis and Recommendations**

To provide a clearer understanding of the different memory management solutions, a comparison of LangMem, MemGPT, and Zep is presented in the table below:

| Feature | LangMem | MemGPT | Zep |
| :---- | :---- | :---- | :---- |
| **Core Concept** | SDK for long-term memory | "LLM Operating System" for memory management | Temporal Knowledge Graph service |
| **Memory Types** | Semantic, Episodic, Procedural | Core (in-context), Archival (out-of-context) | Episodic, Semantic, Temporal context in Knowledge Graph |
| **Storage** | Storage-agnostic (supports various backends) | Primarily vector database for archival memory | Temporal Knowledge Graph |
| **Key Features** | Hot-path memory tools, background memory manager, prompt optimization | Self-editing memory, tool utilization, extended context window management | Captures conversational context, business data integration, fact ratings, efficient retrieval |
| **Framework Integration** | Native with LangGraph, integrates with other frameworks via Core API | Strong integration with AutoGen | Integrations with LangChain, AutoGen, Python, TypeScript, Go SDKs |
| **Focus** | General-purpose long-term memory for AI agents | Overcoming context window limitations, building autonomous agents | Enterprise-grade long-term memory with temporal reasoning |
| **Deployment** | Library | Library, Server (via Docker) | Cloud service, Self-hosted |

Each of these memory management solutions presents its own set of strengths and potential weaknesses. LangMem offers significant flexibility due to its storage-agnostic design and strong integration with the LangChain ecosystem, particularly LangGraph. However, it might require more manual effort for integration with frameworks outside of LangChain. MemGPT excels at extending the context window of LLMs and enabling self-editing memory, making it well-suited for building highly autonomous agents, although its performance can be influenced by the underlying LLM's function calling capabilities. Zep provides a structured and powerful approach to memory management through its temporal knowledge graph, with a strong focus on enterprise applications and efficient retrieval, but its knowledge graph concept might involve a steeper initial learning curve.

The selection of the most appropriate memory solution depends on the specific requirements of the application. Developers already utilizing LangChain or LangGraph might find LangMem or LangChain's built-in memory types to be the most convenient options. For applications where extending the LLM's context window is a primary concern and the focus is on creating autonomous agents, MemGPT could be a strong contender. If the application demands a structured representation of memory with temporal reasoning capabilities and robust features for enterprise use, Zep warrants serious consideration. For users seeking simpler, lightweight alternatives, projects like Memoripy or Mem0 might be worth exploring. Factors such as the existing agent framework, the types of memory required, the preferred deployment environment, the level of control needed, and the performance requirements should all be taken into account when making this decision.

## **Conclusion: The Future of Long-Term Memory in Intelligent AI Agents**

In conclusion, long-term memory is a critical component for building truly intelligent and adaptive AI agents. LangMem offers a versatile SDK for integrating such capabilities, with the flexibility to be used with various agent frameworks, including the OpenAI Agents SDK. By wrapping LangMem's core functionalities as tools, developers can empower their OpenAI agents to retain knowledge and context across interactions. However, the landscape of AI agent memory solutions is rich and diverse, with frameworks like MemGPT and Zep providing alternative architectural approaches and feature sets tailored to different needs. MemGPT focuses on extending context windows and enabling agent autonomy, while Zep leverages a temporal knowledge graph for structured and temporally aware memory management, particularly suited for enterprise applications. Other solutions like LangChain's built-in memory and emerging projects like Memoripy and Mem0 offer further options depending on specific requirements and preferences. As the field of AI continues to advance, we can anticipate the development of even more sophisticated memory architectures, improved integration with multimodal data, and advancements in memory consolidation and retrieval techniques, paving the way for increasingly intelligent and context-aware AI agents.

Based on recent evaluations, Zep appears to be a strong contender, particularly for applications requiring high performance and temporal awareness. LangMem also looks like a good alternative.

### Zep:
* Strong performance in benchmarks, especially regarding latency.   
* Utilizes a temporal knowledge graph for robust memory management.   
* Designed to be framework-independent.
* Shows strong results in recent testing, outperforming MemGPT in many tests.
* The only issue we see with Zep is it cost, therefore one option might be to only use the open source Graphiti, which it is built upon.

### LangMem
* It is free and open source
* We can use any datastore
* Only it integration with Open Agents SDK might be an issue, we will only know when we integrate it core API.

   







