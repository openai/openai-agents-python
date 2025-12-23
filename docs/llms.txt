# OpenAI Agents SDK Documentation

> Official documentation for building production-ready agentic applications with the OpenAI Agents SDK, a Python toolkit that equips LLM-powered assistants with tools, guardrails, handoffs, sessions, tracing, voice, and realtime capabilities.

The SDK focuses on a concise set of primitives so you can orchestrate multi-agent workflows without heavy abstractions. These pages explain how to install the library, design agents, coordinate tools, handle results, and extend the platform to new modalities.

## Start Here
- [Overview](https://openai.github.io/openai-agents-python/): Learn the core primitives—agents, handoffs, guardrails, sessions, and tracing—and see a minimal hello-world example.
- [Quickstart](https://openai.github.io/openai-agents-python/quickstart/): Step-by-step setup for installing the package, configuring API keys, and running your first agent locally.
- [Example Gallery](https://openai.github.io/openai-agents-python/examples/): Task-oriented examples that demonstrate agent loops, tool usage, guardrails, and integration patterns.

## Core Concepts
- [Agents](https://openai.github.io/openai-agents-python/agents/): Configure agent instructions, tools, guardrails, memory, and streaming behavior.
- [Running agents](https://openai.github.io/openai-agents-python/running_agents/): Learn synchronous, asynchronous, and batched execution, plus cancellation and error handling.
- [Sessions](https://openai.github.io/openai-agents-python/sessions/): Manage stateful conversations with automatic history persistence and memory controls.
- [Results](https://openai.github.io/openai-agents-python/results/): Inspect agent outputs, tool calls, follow-up actions, and metadata returned by the runner.
- [Streaming](https://openai.github.io/openai-agents-python/streaming/): Stream intermediate tool usage and LLM responses for responsive UIs.
- [REPL](https://openai.github.io/openai-agents-python/repl/): Use the interactive runner to prototype agents and inspect execution step by step.
- [Context strategies](https://openai.github.io/openai-agents-python/context/): Control what past messages, attachments, and tool runs are injected into prompts.

## Coordination and Safety
- [Handoffs](https://openai.github.io/openai-agents-python/handoffs/): Delegate tasks between agents with intent classification, argument passing, and return values.
- [Multi-agent patterns](https://openai.github.io/openai-agents-python/multi_agent/): Architect teams of agents that collaborate, escalate, or specialize by capability.
- [Guardrails](https://openai.github.io/openai-agents-python/guardrails/): Define validators that run alongside the agent loop to enforce business and safety rules.
- [Tools](https://openai.github.io/openai-agents-python/tools/): Register Python callables as structured tools, manage schemas, and work with tool contexts.
- [Model Context Protocol](https://openai.github.io/openai-agents-python/mcp/): Connect MCP servers so agents can request external data or actions through standardized tool APIs.

## Operations and Configuration
- [Usage and pricing](https://openai.github.io/openai-agents-python/usage/): Understand token accounting, usage metrics, and cost estimation.
- [Configuration](https://openai.github.io/openai-agents-python/config/): Tune model selection, retry logic, rate limits, and runner policies for production workloads.
- [Visualization](https://openai.github.io/openai-agents-python/visualization/): Embed tracing dashboards and visualize agent runs directly in notebooks and web apps.

## Observability and Tracing
- [Tracing](https://openai.github.io/openai-agents-python/tracing/): Capture spans for every agent step, emit data to OpenAI traces, and integrate third-party processors.

## Modalities and Interfaces
- [Voice quickstart](https://openai.github.io/openai-agents-python/voice/quickstart/): Build speech-enabled agents with streaming transcription and TTS.
- [Voice pipeline](https://openai.github.io/openai-agents-python/voice/pipeline/): Customize audio ingestion, tool execution, and response rendering.
- [Realtime quickstart](https://openai.github.io/openai-agents-python/realtime/quickstart/): Stand up low-latency realtime agents with websocket transport (WebRTC is not available in the Python SDK).
- [Realtime guide](https://openai.github.io/openai-agents-python/realtime/guide/): Deep dive into session lifecycle, event formats, and concurrency patterns.

## API Reference Highlights
- [Agents API index](https://openai.github.io/openai-agents-python/ref/index/): Entry point for class and function documentation throughout the SDK.
- [Agent lifecycle](https://openai.github.io/openai-agents-python/ref/lifecycle/): Understand the runner, evaluation phases, and callbacks triggered during execution.
- [Runs and sessions](https://openai.github.io/openai-agents-python/ref/run/): API for launching runs, streaming updates, and handling cancellations.
- [Results objects](https://openai.github.io/openai-agents-python/ref/result/): Data structures returned from agent runs, including final output and tool calls.
- [Tool interfaces](https://openai.github.io/openai-agents-python/ref/tool/): Create tools, parse arguments, and manage tool execution contexts.
- [Tracing APIs](https://openai.github.io/openai-agents-python/ref/tracing/index/): Programmatic interfaces for creating traces, spans, and integrating custom processors.
- [Realtime APIs](https://openai.github.io/openai-agents-python/ref/realtime/agent/): Classes for realtime agents, runners, sessions, and event payloads.
- [Voice APIs](https://openai.github.io/openai-agents-python/ref/voice/pipeline/): Configure voice pipelines, inputs, events, and model adapters.
- [Extensions](https://openai.github.io/openai-agents-python/ref/extensions/handoff_filters/): Extend the SDK with custom handoff filters, prompts, LiteLLM integration, and SQLAlchemy session memory.

## Models and Providers
- [Model catalog](https://openai.github.io/openai-agents-python/models/): Overview of supported model families and configuration guidance.
- [LiteLLM integration](https://openai.github.io/openai-agents-python/models/litellm/): Configure LiteLLM as a provider to fan out across multiple model backends.

## Optional
- [Release notes](https://openai.github.io/openai-agents-python/release/): Track SDK changes, migration notes, and deprecations.
- [Japanese documentation](https://openai.github.io/openai-agents-python/ja/): Localized overview and quickstart for Japanese-speaking developers.
- [Repository on GitHub](https://github.com/openai/openai-agents-python): Source code, issues, and contribution guidelines for the SDK.
