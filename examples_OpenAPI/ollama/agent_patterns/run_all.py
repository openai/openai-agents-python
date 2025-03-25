import sys
import os
import importlib
import asyncio

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

# List of available examples
EXAMPLES = {
    "routing": "Routing/Triage Example - Automatically select specialized agents based on language",
    "deterministic": "Deterministic Workflow Example - Execute multiple steps in sequence",
    "parallel": "Parallel Execution Example - Generate multiple results in parallel and select the best",
    "agents_as_tools": "Agents as Tools Example - Use other agents as tools",
    "llm_as_judge": "LLM as Judge Example - Use one agent to evaluate another's output",
    "input_guardrails": "Input Guardrails Example - Detect inappropriate user input",
    "output_guardrails": "Output Guardrails Example - Ensure model output doesn't contain sensitive data",
    "forcing_tool_use": "Forcing Tool Use Example - Ensure model uses tools",
}

# Special options for forcing_tool_use
TOOL_BEHAVIORS = ["default", "first_tool", "custom"]

def check_ollama_running():
    """Check if Ollama service is running"""
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned a non-200 status code. Make sure Ollama service is running.")
            return False
    except Exception as e:
        print(f"Error: Could not connect to Ollama service. Make sure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama yet, download and install it from https://ollama.ai and start the service with 'ollama serve'")
        return False
    return True

async def run_example(example_name):
    """Run a single example"""
    print(f"\n\n{'='*60}")
    print(f"    Running example: {example_name} - {EXAMPLES[example_name]}")
    print(f"{'='*60}")
    
    try:
        # Special handling for forcing_tool_use example
        if example_name == "forcing_tool_use":
            # Run all three modes for forcing_tool_use
            for behavior in TOOL_BEHAVIORS:
                print(f"\n>> Using mode: {behavior}")
                module = importlib.import_module(example_name)
                await module.main(behavior)
        else:
            # Import and run example
            module = importlib.import_module(example_name)
            await module.main()
        return True
    except Exception as e:
        print(f"Error running {example_name}: {str(e)}")
        return False

async def main():
    # Check Ollama service
    if not check_ollama_running():
        return
    
    print("\n===== Ollama Agent Pattern Examples Runner Tool =====\n")
    print("Options:")
    print("1. Run all examples")
    print("2. Select a single example to run")
    
    choice = input("\nSelect operation (1/2): ")
    
    if choice == "1":
        # Run all examples
        print("\nWill run all examples in sequence...")
        successes = 0
        failures = 0
        
        for example_name in EXAMPLES.keys():
            success = await run_example(example_name)
            if success:
                successes += 1
            else:
                failures += 1
            
            # Pause between examples if not the last one
            if example_name != list(EXAMPLES.keys())[-1]:
                input("\nPress Enter to continue to the next example...")
        
        print(f"\n\nAll examples completed. Successes: {successes}, Failures: {failures}")
    
    elif choice == "2":
        # Display available examples
        for i, (name, desc) in enumerate(EXAMPLES.items(), 1):
            print(f"{i}. {name}: {desc}")
        
        # Get user selection
        while True:
            try:
                ex_choice = input("\nSelect an example to run (number or name, q to quit): ")
                if ex_choice.lower() == 'q':
                    return
                    
                # Handle numeric input
                if ex_choice.isdigit() and 1 <= int(ex_choice) <= len(EXAMPLES):
                    example_name = list(EXAMPLES.keys())[int(ex_choice) - 1]
                    await run_example(example_name)
                    break
                
                # Handle name input
                if ex_choice in EXAMPLES:
                    await run_example(ex_choice)
                    break
                    
                print("Invalid selection, please try again.")
            except (ValueError, IndexError):
                print("Invalid selection, please try again.")
    
    else:
        print("Invalid choice")

if __name__ == "__main__":
    asyncio.run(main())
