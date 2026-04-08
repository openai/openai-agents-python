"""Example: Prometheus metrics endpoint for agent monitoring.

This example shows how to set up a FastAPI server with a /metrics endpoint
that exposes Prometheus metrics for your agents.

To run:
    pip install 'openai-agents[prometheus]' fastapi uvicorn
    uv run python examples/metrics/prometheus_endpoint.py

Then open http://localhost:8000/metrics in your browser or configure
Prometheus to scrape http://localhost:8000/metrics
"""

from __future__ import annotations

import asyncio
import time
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from agents import Agent, Runner
from agents.metrics import PrometheusMetrics, MetricsHooks, enable_metrics

metrics = PrometheusMetrics()
enable_metrics(metrics)

metrics_app = make_asgi_app()

agent = Agent(
    name="math_assistant",
    instructions="You are a helpful math assistant. Solve simple math problems.",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    print("Starting server with metrics enabled...")
    print("Visit http://localhost:8000/metrics for Prometheus metrics")
    yield
    print("Shutting down...")


app = FastAPI(title="Agent Metrics Example", lifespan=lifespan)

app.mount("/metrics", metrics_app)


@app.get("/")
async def root():
    """Root endpoint with instructions."""
    return {
        "message": "Agent Metrics Example",
        "endpoints": {
            "/": "This help message",
            "/metrics": "Prometheus metrics endpoint",
            "/solve/{problem}": "Solve a math problem (generates metrics)",
            "/chat/{message}": "Chat with the agent (generates metrics)",
        },
    }


@app.get("/solve/{problem}")
async def solve(problem: str):
    """Solve a math problem and record metrics."""
    hooks = MetricsHooks()

    start_time = time.monotonic()

    try:
        result = await Runner.run(
            agent,
            f"Solve this math problem: {problem}",
            hooks=[hooks],
        )

        duration = time.monotonic() - start_time

        return {
            "problem": problem,
            "solution": result.final_output,
            "duration_seconds": round(duration, 3),
        }
    except Exception as e:
        duration = time.monotonic() - start_time
        return {
            "problem": problem,
            "error": str(e),
            "duration_seconds": round(duration, 3),
        }


@app.get("/chat/{message}")
async def chat(message: str):
    """Chat with the agent and record metrics."""
    hooks = MetricsHooks()

    try:
        result = await Runner.run(
            agent,
            message,
            hooks=[hooks],
        )

        return {
            "message": message,
            "response": result.final_output,
            "usage": {
                "input_tokens": result.usage.input_tokens if result.usage else 0,
                "output_tokens": result.usage.output_tokens if result.usage else 0,
                "total_tokens": result.usage.total_tokens if result.usage else 0,
            },
        }
    except Exception as e:
        return {
            "message": message,
            "error": str(e),
        }


@app.post("/generate-load")
async def generate_load(count: int = 10):
    """Generate load for testing metrics (simulated)."""
    results = []

    for i in range(count):
        operation = random.choice(["add", "multiply", "divide", "subtract"])
        a, b = random.randint(1, 100), random.randint(1, 100)

        latency = random.uniform(0.1, 2.0)
        tokens_in = random.randint(50, 500)
        tokens_out = random.randint(20, 200)

        metrics.record_llm_call(
            latency=latency,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model="gpt-4",
        )

        if random.random() < 0.1:
            error_type = random.choice(["RateLimitError", "TimeoutError", "APIError"])
            metrics.record_error(error_type, agent.name or "unknown")
            results.append(
                {
                    "operation": operation,
                    "error": error_type,
                }
            )
        else:
            results.append(
                {
                    "operation": operation,
                    "a": a,
                    "b": b,
                    "latency": round(latency, 3),
                }
            )

        await asyncio.sleep(0.01)

    return {
        "generated": count,
        "results": results,
    }


if __name__ == "__main__":
    import uvicorn

    print("""
  Endpoints:                                                  
    • http://localhost:8000/           - API documentation      
    • http://localhost:8000/metrics    - Prometheus metrics     
    • http://localhost:8000/solve/{x}  - Solve math problem     
    • http://localhost:8000/chat/{msg} - Chat with agent        
    • POST /generate-load?count=10     - Generate test load     
                                                              
  Metrics available:                                          
    • agents_llm_latency_seconds     - LLM call latency         
    • agents_tokens_total            - Token usage              
    • agents_errors_total            - Error counts             
    • agents_runs_total              - Run counts               
    • agents_run_duration_seconds    - Run duration             
    • agents_turns_total             - LLM turns                
    • agents_tool_executions_total   - Tool executions          
    • agents_tool_latency_seconds    - Tool latency             
                                                              
    """)

    uvicorn.run(app, host="0.0.0.0", port=8000)
