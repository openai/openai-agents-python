# Keywords AI

This folder contains examples demonstrating the use of Keywords AI for monitoring and analyzing LLM interactions.

## Official Resources
- [OpenAI Agents SDK Documentation](https://openai.github.io/openai-agents-python/tracing/)
- [Keywords AI Documentation](https://docs.keywords.ai/)

## Features
- LLM interaction monitoring
- Keyword analysis
- Performance tracking
- Usage analytics
- Custom keyword detection

## Setup Requirements
- OpenAI Agents SDK installed
- Keywords AI account
- Python environment with async support

## Example Structure
This folder will contain examples showing:
1. Basic Keywords AI setup
2. Keyword monitoring
3. Performance tracking
4. Usage analysis
5. Custom keyword detection

## OpenAI Agents SDK Example with Keywords AI
```python
from agents import Agent, Runner, trace
from keywords_ai import KeywordsAI

async def main():
    # Initialize Keywords AI
    keywords_ai = KeywordsAI()
    
    # Create an agent
    agent = Agent(
        name="Code Analyzer",
        instructions="Analyze code and identify key patterns."
    )

    # Create a trace for the analysis workflow
    with trace("Code Analysis Workflow") as analysis_trace:
        # Run the agent
        result = await Runner.run(
            agent,
            "Analyze this code: def process_data(data): return data.strip()"
        )
        
        # Analyze keywords in the response
        keyword_analysis = await keywords_ai.analyze(
            text=result.final_output,
            keywords=["function", "class", "method", "variable", "import"]
        )
        
        # Log metrics
        keywords_ai.log_metric("keyword_count", len(keyword_analysis.keywords))
        keywords_ai.log_metric("unique_keywords", len(set(keyword_analysis.keywords)))
        
        print(f"Analysis: {result.final_output}")
        print(f"Keywords found: {keyword_analysis}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

## Multi-Keyword Analysis Example
```python
from agents import Agent, Runner, trace
from keywords_ai import KeywordsAI

async def main():
    keywords_ai = KeywordsAI()
    
    # Create specialized agents
    code_agent = Agent(
        name="Code Generator",
        instructions="Generate Python code based on requirements."
    )
    
    analysis_agent = Agent(
        name="Code Analyzer",
        instructions="Analyze code and identify patterns."
    )

    # Create a trace for the multi-keyword analysis workflow
    with trace("Code Generation and Analysis Workflow"):
        # Step 1: Generate code
        code_result = await Runner.run(
            code_agent,
            "Write a function to calculate fibonacci numbers"
        )
        
        # Step 2: Analyze code
        analysis_result = await Runner.run(
            analysis_agent,
            f"Analyze this code: {code_result.final_output}"
        )
        
        # Analyze multiple keyword categories
        analyses = await keywords_ai.analyze_multi(
            text=analysis_result.final_output,
            keyword_categories={
                "code_structure": ["function", "class", "method", "variable"],
                "complexity": ["loop", "recursion", "condition", "exception"],
                "patterns": ["singleton", "factory", "observer", "decorator"]
            }
        )
        
        # Log all keyword metrics
        for category, analysis in analyses.items():
            keywords_ai.log_metric(f"{category}_keyword_count", len(analysis.keywords))
            keywords_ai.log_metric(f"{category}_unique_keywords", len(set(analysis.keywords)))
        
        print(f"Generated code: {code_result.final_output}")
        print(f"Analysis: {analysis_result.final_output}")
        print(f"Keyword analyses: {analyses}")
        
        # Get keyword statistics
        stats = keywords_ai.get_statistics()
        print(f"Keyword statistics: {stats}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main()) 