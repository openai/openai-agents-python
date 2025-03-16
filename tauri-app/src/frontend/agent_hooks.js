import { AgentHooks } from 'openai-agents';

class CustomAgentHooks extends AgentHooks {
  constructor(displayName) {
    super();
    this.eventCounter = 0;
    this.displayName = displayName;
  }

  async on_start(context, agent) {
    this.eventCounter += 1;
    console.log(`### (${this.displayName}) ${this.eventCounter}: Agent ${agent.name} started`);
  }

  async on_end(context, agent, output) {
    this.eventCounter += 1;
    console.log(`### (${this.displayName}) ${this.eventCounter}: Agent ${agent.name} ended with output ${output}`);
  }

  async on_handoff(context, agent, source) {
    this.eventCounter += 1;
    console.log(`### (${this.displayName}) ${this.eventCounter}: Agent ${source.name} handed off to ${agent.name}`);
  }

  async on_tool_start(context, agent, tool) {
    this.eventCounter += 1;
    console.log(`### (${this.displayName}) ${this.eventCounter}: Agent ${agent.name} started tool ${tool.name}`);
  }

  async on_tool_end(context, agent, tool, result) {
    this.eventCounter += 1;
    console.log(`### (${this.displayName}) ${this.eventCounter}: Agent ${agent.name} ended tool ${tool.name} with result ${result}`);
  }
}

export default CustomAgentHooks;
