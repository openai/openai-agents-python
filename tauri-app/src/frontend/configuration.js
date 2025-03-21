import { validate } from 'pydantic';
import { GuardrailFunctionOutput, InputGuardrail, OutputGuardrail } from 'openai-agents';

export function configureAgentGuardrails(agent, inputGuardrails, outputGuardrails) {
  agent.input_guardrails = inputGuardrails.map(guardrail => new InputGuardrail(guardrail));
  agent.output_guardrails = outputGuardrails.map(guardrail => new OutputGuardrail(guardrail));
}

export function configureAgentHandoffs(agent, handoffs) {
  agent.handoffs = handoffs.map(handoffData => handoff(handoffData));
}

export function configureAgentTools(agent, tools) {
  agent.tools = tools.map(tool => function_tool(tool));
}

export function provideAgentTemplates() {
  return [
    {
      name: 'Basic Agent',
      instructions: 'You are a basic agent.',
      model: 'text-davinci-003',
      tools: [],
      context: {},
      outputTypes: 'text',
      handoffs: [],
      guardrails: {
        input: [],
        output: [],
      },
      cloning: false,
    },
    {
      name: 'Advanced Agent',
      instructions: 'You are an advanced agent with multiple tools and guardrails.',
      model: 'text-davinci-003',
      tools: ['tool1', 'tool2'],
      context: {},
      outputTypes: 'json',
      handoffs: ['handoff1', 'handoff2'],
      guardrails: {
        input: ['inputGuardrail1', 'inputGuardrail2'],
        output: ['outputGuardrail1', 'outputGuardrail2'],
      },
      cloning: true,
    },
  ];
}

export function validateAgentConfiguration(agentData) {
  const schema = {
    name: 'string',
    instructions: 'string',
    model: 'string',
    tools: 'array',
    context: 'object',
    outputTypes: 'string',
    handoffs: 'array',
    guardrails: {
      input: 'array',
      output: 'array',
    },
    cloning: 'boolean',
  };

  return validate(agentData, schema);
}
