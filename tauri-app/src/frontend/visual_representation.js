import { getAllAgents } from './agent_management';

document.addEventListener("DOMContentLoaded", () => {
  const visualRepresentationContainer = document.getElementById("visual-representation");

  function displayVisualRepresentation() {
    const agents = getAllAgents();
    visualRepresentationContainer.innerHTML = "";

    agents.forEach(agent => {
      const agentItem = document.createElement("div");
      agentItem.className = "agent-item";
      agentItem.innerHTML = `
        <h3>${agent.name}</h3>
        <p>Tools: ${agent.tools.map(tool => tool.name).join(", ")}</p>
        <p>Handoffs: ${agent.handoffs.map(handoff => handoff.name).join(", ")}</p>
      `;
      visualRepresentationContainer.appendChild(agentItem);
    });
  }

  displayVisualRepresentation();
});
