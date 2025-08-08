Bidirectional Handoffs in OpenAI Agents SDK
Hey there! 😊 Welcome to the world of bidirectional handoffs—a super cool feature in the OpenAI Agents SDK that lets sub-agents pass control back to their parent agent. This makes it easy to build powerful, orchestrator-style workflows where a central agent coordinates multiple specialized agents. Think of it like a team leader delegating tasks to experts and getting their reports back to decide what’s next! This feature was built to address Issue #1376 and makes your agent workflows more flexible and scalable.
What Are Bidirectional Handoffs?
In the old days, handoffs were one-way: a parent agent could send tasks to sub-agents, but sub-agents couldn’t talk back. Now, with bidirectional handoffs:

A parent (orchestrator) agent can delegate tasks to specialized sub-agents.
Sub-agents can complete their tasks and return control to the parent with their results.
The parent can then decide what to do next—maybe hand off to another sub-agent or wrap things up.
This creates dynamic, non-linear workflows that are perfect for complex tasks like financial analysis, customer support, or research pipelines.

Why You’ll Love It

Orchestrator Power: One agent can manage multiple sub-agents, like a conductor leading an orchestra.
Flexible Workflows: Sub-agents can return results, letting the parent decide the next step.
Scalable with Registry: Use AgentRegistry to manage agents and workflows efficiently.
Backward Compatible: Works seamlessly with existing linear handoffs.
Customizable: Enable/disable return-to-parent, add filters, or customize handoff tools.

Getting Started
Let’s jump into how you can use bidirectional handoffs! We’ll start simple and then explore some advanced tricks.
Basic Setup: Parent-Child Relationships
You can manually set up a parent-child relationship and add a return-to-parent handoff:
from openai.agents import Agent
from openai.agents.handoffs import handoff, return_to_parent_handoff

# Create parent and sub-agent
orchestrator = Agent(name="Orchestrator", instructions="Coordinate tasks.")
financial_agent = Agent(name="FinancialAgent", instructions="Analyze financial data.")

# Set parent-child relationship
financial_agent.set_parent(orchestrator)

# Add handoffs
orchestrator.handoffs.append(handoff(financial_agent))
financial_agent.handoffs.append(return_to_parent_handoff(orchestrator))

This sets up a workflow where the orchestrator can delegate to the financial agent, and the financial agent can return control with its results.
Easy Mode: Using the Helper Function
For a quicker setup, use the create_bidirectional_handoff_workflow helper:
from openai.agents import Agent
from openai.agents.handoffs import create_bidirectional_handoff_workflow

# Create agents
orchestrator = Agent(name="Orchestrator", instructions="Coordinate workflows.")
financial_agent = Agent(name="FinancialAgent", instructions="Fetch financial data.")
docs_agent = Agent(name="DocsAgent", instructions="Save documents.")

# Set up bidirectional workflow
orchestrator, sub_agents = create_bidirectional_handoff_workflow(
    orchestrator_agent=orchestrator,
    sub_agents=[financial_agent, docs_agent],
    enable_return_to_parent=True
)

This automatically:

Sets the orchestrator as the parent of both sub-agents.
Adds handoffs from the orchestrator to each sub-agent.
Adds return-to-parent handoffs for each sub-agent.

Running a Workflow
Use AgentRunner with AgentRegistry to run your workflow:
from openai.agents.registry import AgentRegistry, AgentRunner
from openai.agents import AgentContext

# Create and configure registry
registry = AgentRegistry()
registry.register("Orchestrator", {"instructions": "Coordinate tasks"})
registry.register("FinancialAgent", {"instructions": "Fetch data"}, parent_name="Orchestrator")
registry.register("DocsAgent", {"instructions": "Save docs"}, parent_name="Orchestrator")
registry.register_workflow("financial_research", "Orchestrator", ["FinancialAgent", "DocsAgent"])

# Run the workflow
runner = AgentRunner(registry)
result = await runner.run_workflow(
    workflow_name="financial_research",
    input_task="Fetch AAPL financials and save to a Google Doc.",
    context=AgentContext(),
    max_turns=10
)

print(f"Result: {result.final_output}")
print(f"Agent History: {result.agent_history}")

Advanced Usage
Ready to level up? Here are some advanced features to make your workflows even more powerful.
Customizing Return-to-Parent
You can customize the return-to-parent handoff with your own tool names and descriptions:
return_handoff = return_to_parent_handoff(
    orchestrator,
    tool_name_override="complete_task",
    tool_description_override="Finish task and return to orchestrator."
)
financial_agent.handoffs.append(return_handoff)

Conditional Handoffs
Make handoffs conditional based on context or agent state:
def should_return(ctx, agent):
    return "task_completed" in ctx.context and agent.name == "FinancialAgent"

return_handoff = return_to_parent_handoff(orchestrator, is_enabled=should_return)
financial_agent.handoffs.append(return_handoff)

Filtering Results
Control what data is passed back to the parent with an input filter:
def filter_results(handoff_data):
    # Only send the latest results
    return handoff_data.clone(new_items=handoff_data.new_items[-2:])

return_handoff = return_to_parent_handoff(orchestrator, input_filter=filter_results)
financial_agent.handoffs.append(return_handoff)

Parallel Execution
Run multiple sub-agents at the same time:
registry = AgentRegistry()
registry.register("Agent1", {"instructions": "Task 1"})
registry.register("Agent2", {"instructions": "Task 2"})

runner = AgentRunner(registry)
tasks = [
    ("Agent1", "Analyze data"),
    ("Agent2", "Generate report")
]
results = await runner.run_parallel(tasks, context=AgentContext(), max_turns=5)

for result in results:
    print(f"Task Result: {result.final_output}")

Real-World Examples
Financial Research Workflow
Imagine you need to fetch financial data and save it to a Google Doc:
registry = AgentRegistry()
registry.register("Orchestrator", {"instructions": "Coordinate research"})
registry.register("FinancialAgent", {"instructions": "Fetch data"}, parent_name="Orchestrator")
registry.register("DocsAgent", {"instructions": "Save docs"}, parent_name="Orchestrator")
registry.register_workflow("financial_research", "Orchestrator", ["FinancialAgent", "DocsAgent"])

runner = AgentRunner(registry)
result = await runner.run_workflow(
    workflow_name="financial_research",
    input_task="Fetch AAPL financials and save to 'Apple Financials' doc.",
    context=AgentContext(),
    max_turns=10
)

The orchestrator delegates to FinancialAgent to fetch data, which returns results. The orchestrator then hands off to DocsAgent to save the results.
Customer Support Workflow
For a support system:
registry = AgentRegistry()
registry.register("TriageAgent", {"instructions": "Route customer issues"})
registry.register("BillingAgent", {"instructions": "Handle billing"}, parent_name="TriageAgent")
registry.register("TechnicalAgent", {"instructions": "Handle tech issues"}, parent_name="TriageAgent")
registry.register_workflow("support", "TriageAgent", ["BillingAgent", "TechnicalAgent"])

runner = AgentRunner(registry)
result = await runner.run_workflow(
    workflow_name="support",
    input_task="Customer reports billing error.",
    context=AgentContext(),
    max_turns=10
)

The TriageAgent routes to BillingAgent, which returns control after resolving the issue.
Best Practices

Clear Instructions: Tell sub-agents when to return control:
sub_agent = Agent(
    name="SubAgent",
    instructions="Complete your task, then use return_to_parent to send results back."
)


Orchestrator Logic: Make sure the orchestrator knows what to do with returned results:
orchestrator = Agent(
    name="Orchestrator",
    instructions="Delegate tasks, evaluate sub-agent results, and decide next steps."
)


Prevent Loops: Set max_turns in Runner.run() or AgentRunner.run() to avoid infinite loops.

Use Registry: Use AgentRegistry for scalable agent management:
registry = AgentRegistry()
registry.register("MyAgent", {"instructions": "Do stuff"})


Error Handling: Make sub-agents return to the parent if they can’t complete tasks:
sub_agent = Agent(
    name="SubAgent",
    instructions="If you can’t solve the task, return to parent with an error message."
)



Migrating from Linear Handoffs
If you’re using old-school linear handoffs, upgrading is easy:
Before (Linear):
agent_a.handoffs.append(handoff(agent_b))
agent_b.handoffs.append(handoff(agent_c))

After (Bidirectional):
registry = AgentRegistry()
registry.register("Orchestrator", {"instructions": "Coordinate"})
registry.register("AgentB", {"instructions": "Task B"}, parent_name="Orchestrator")
registry.register("AgentC", {"instructions": "Task C"}, parent_name="Orchestrator")
registry.register_workflow("my_workflow", "Orchestrator", ["AgentB", "AgentC"])

runner = AgentRunner(registry)
result = await runner.run_workflow("my_workflow", input_task="Do tasks B and C")

Troubleshooting

Sub-agent not returning control? Check if return_to_parent_handoff is added and instructions are clear.
Orchestrator not delegating? Ensure handoffs are set up and the orchestrator’s logic picks the right sub-agent.
Infinite loops? Set max_turns in Runner.run() or AgentRunner.run().
Missing parent? Use set_parent() or create_bidirectional_handoff_workflow().

Debugging tip:
print(f"Agent: {agent.name}, Parent: {agent.parent.name if agent.parent else 'None'}")
for h in agent.handoffs:
    print(f"Handoff: {h.tool_name} -> {h.agent_name} (Return: {h.is_return_to_parent})")

API Reference

Agent:

parent: Agent[Any] | None - Weak reference to parent agent.
return_to_parent_enabled: bool - Enable/disable return-to-parent.
set_parent(parent: Agent[Any]) - Set parent agent.
can_return_to_parent() -> bool - Check if return-to-parent is possible.


Handoff:

is_return_to_parent: bool - Indicates a return-to-parent handoff.


Functions:

return_to_parent_handoff(parent, **kwargs) -> Handoff: Create a return-to-parent handoff.
create_bidirectional_handoff_workflow(orchestrator, sub_agents, **kwargs) -> tuple: Set up a bidirectional workflow.
AgentRegistry: Manage agents and workflows.
AgentRunner: Run workflows and parallel tasks.



Related Issues
This feature resolves Issue #1376: Bidirectional Handoff System.
Happy building, and let us know if you run into any issues! 🚀