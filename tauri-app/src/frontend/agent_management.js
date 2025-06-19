import { Agent, Runner, function_tool, handoff, GuardrailFunctionOutput, RunContextWrapper } from 'openai-agents';
import CustomAgentHooks from './agent_hooks';

const agents = {};

export function createAgent(agentData) {
  const {
    name,
    instructions,
    model,
    tools,
    context,
    outputTypes,
    handoffs,
    guardrails,
    cloning,
  } = agentData;

  const agent = new Agent({
    name,
    instructions,
    model,
    tools: tools.map(tool => function_tool(tool)),
    context: new RunContextWrapper(context),
    output_type: outputTypes,
    handoffs: handoffs.map(handoffData => handoff(handoffData)),
    input_guardrails: guardrails.input.map(guardrail => new GuardrailFunctionOutput(guardrail)),
    output_guardrails: guardrails.output.map(guardrail => new GuardrailFunctionOutput(guardrail)),
    hooks: new CustomAgentHooks(name),
  });

  if (cloning) {
    agent.clone();
  }

  agents[name] = agent;
}

export function runAgent(agentName, input) {
  const agent = agents[agentName];
  if (!agent) {
    throw new Error(`Agent with name ${agentName} does not exist.`);
  }

  return Runner.run(agent, input);
}

export function getAgent(agentName) {
  return agents[agentName];
}

export function getAllAgents() {
  return Object.values(agents);
}
