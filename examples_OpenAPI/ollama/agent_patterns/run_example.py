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
    "parallelization": "Parallel Execution Example - Generate multiple results in parallel and select the best",
    "agents_as_tools": "Agents as Tools Example - Use other agents as tools",
    "llm_as_judge": "LLM as Judge Example - Use one agent to evaluate another's output",
    "input_guardrails": "Input Guardrails Example - Detect inappropriate user input",
    "output_guardrails": "Output Guardrails Example - Ensure model output doesn't contain sensitive data",
    "forcing_tool_use": "Forcing Tool Use Example - Ensure model uses tools",
}

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

async def main():
    # Check Ollama service
    if not check_ollama_running():
        return
    
    # Display available examples
    print("\n===== Ollama Agent Pattern Examples =====\n")
    for i, (name, desc) in enumerate(EXAMPLES.items(), 1):
        print(f"{i}. {name}: {desc}")
    
    # Get user selection
    while True:
        try:
            choice = input("\nSelect an example to run (number or name, q to quit): ")
            if choice.lower() == 'q':
                return
                
            # Handle numeric input
            if choice.isdigit() and 1 <= int(choice) <= len(EXAMPLES):
                example_name = list(EXAMPLES.keys())[int(choice) - 1]
                break
            
            # Handle name input
            if choice in EXAMPLES:
                example_name = choice
                break
                
            print("Invalid selection, please try again.")
        except (ValueError, IndexError):
            print("Invalid selection, please try again.")
    
    print(f"\nStarting example: {example_name}")
    print("=" * 50)
    
    # Handle special case for forcing tool use
    if example_name == "forcing_tool_use":
        # Import module
        module = importlib.import_module(example_name)
        # Get available tool use modes
        tool_behaviors = ["default", "first_tool", "custom"]
        print("Forcing Tool Use example has the following modes:")
        for i, behavior in enumerate(tool_behaviors, 1):
            print(f"{i}. {behavior}")
        
        # Get user selection for mode
        while True:
            try:
                behavior_choice = input("\nSelect mode (number or name): ")
                if behavior_choice.isdigit() and 1 <= int(behavior_choice) <= len(tool_behaviors):
                    behavior = tool_behaviors[int(behavior_choice) - 1]
                    break
                if behavior_choice in tool_behaviors:
                    behavior = behavior_choice
                    break
                print("Invalid selection, please try again.")
            except (ValueError, IndexError):
                print("Invalid selection, please try again.")
                
        # Run example
        await module.main(behavior)
    else:
        # Import and run example
        module = importlib.import_module(example_name)
        await module.main()
    
    print("\nExample execution complete.")

if __name__ == "__main__":
    asyncio.run(main())
