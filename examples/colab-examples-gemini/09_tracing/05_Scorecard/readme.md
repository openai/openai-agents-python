# Scorecard

This folder contains examples demonstrating the use of Scorecard for evaluating and monitoring LLM application performance.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [Scorecard Documentation](https://docs.scorecard.ai/)

## Features
- LLM application evaluation
- Performance metrics
- Quality assessment
- Cost analysis
- Custom evaluation criteria

## Setup Requirements
- OpenAI Agents SDK installed
- Scorecard account
- Python environment with async support

## Example Structure
This folder will contain examples showing:
1. Basic Scorecard setup
2. Custom evaluation metrics
3. Performance monitoring
4. Quality assessment
5. Cost tracking

## OpenAI Agents SDK Example with Scorecard
```python
from agents import Agent, Runner, trace
from scorecard import Scorecard

async def main():
    # Initialize Scorecard
    scorecard = Scorecard()
    
    # Create an agent
    agent = Agent(
        name="Code Quality Evaluator",
        instructions="Evaluate code quality and suggest improvements."
    )

    # Create a trace for the evaluation workflow
    with trace("Code Quality Evaluation Workflow") as eval_trace:
        # Run the agent
        result = await Runner.run(
            agent,
            "Review this code: def add(a,b): return a+b"
        )
        
        # Evaluate the response
        evaluation = await scorecard.evaluate(
            input="def add(a,b): return a+b",
            output=result.final_output,
            metrics=["code_quality", "readability", "maintainability"]
        )
        
        # Log metrics
        scorecard.log_metric("quality_score", evaluation.quality_score)
        scorecard.log_metric("readability_score", evaluation.readability_score)
        scorecard.log_metric("maintainability_score", evaluation.maintainability_score)
        
        print(f"Evaluation: {result.final_output}")
        print(f"Scores: {evaluation}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Multi-Metric Evaluation Example
```python
from agents import Agent, Runner, trace
from scorecard import Scorecard

async def main():
    scorecard = Scorecard()
    
    # Create specialized agents
    code_agent = Agent(
        name="Code Generator",
        instructions="Generate Python code based on requirements."
    )
    
    review_agent = Agent(
        name="Code Reviewer",
        instructions="Review code and evaluate quality."
    )

    # Create a trace for the multi-metric evaluation workflow
    with trace("Code Generation and Review Workflow"):
        # Step 1: Generate code
        code_result = await Runner.run(
            code_agent,
            "Write a function to calculate fibonacci numbers"
        )
        
        # Step 2: Review and evaluate
        review_result = await Runner.run(
            review_agent,
            f"Review this code: {code_result.final_output}"
        )
        
        # Evaluate multiple aspects
        evaluations = await scorecard.evaluate_multi(
            input=code_result.final_output,
            output=review_result.final_output,
            metrics={
                "code_quality": ["complexity", "readability", "maintainability"],
                "performance": ["efficiency", "optimization"],
                "security": ["vulnerabilities", "best_practices"]
            }
        )
        
        # Log all metrics
        for category, scores in evaluations.items():
            for metric, score in scores.items():
                scorecard.log_metric(f"{category}_{metric}", score)
        
        print(f"Generated code: {code_result.final_output}")
        print(f"Review: {review_result.final_output}")
        print(f"Evaluations: {evaluations}")
        
        # Get overall score
        overall_score = scorecard.get_overall_score()
        print(f"Overall score: {overall_score}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main()) 