import { getAllAgents, runAgent, createAgent } from './agent_management';

document.addEventListener("DOMContentLoaded", () => {
  const agentsList = document.getElementById("agents-list");

  function displayAgents() {
    const agents = getAllAgents();
    agentsList.innerHTML = "";

    agents.forEach(agent => {
      const agentItem = document.createElement("div");
      agentItem.className = "agent-item";
      agentItem.innerHTML = `
        <h3>${agent.name}</h3>
        <p>Status: ${agent.status}</p>
        <p>Configuration: ${JSON.stringify(agent.configuration)}</p>
        <p>Recent Activities: ${agent.recentActivities.join(", ")}</p>
        <button class="edit-agent" data-name="${agent.name}">Edit</button>
        <button class="clone-agent" data-name="${agent.name}">Clone</button>
        <button class="delete-agent" data-name="${agent.name}">Delete</button>
      `;
      agentsList.appendChild(agentItem);
    });
  }

  function searchAgents(query) {
    const agents = getAllAgents();
    const filteredAgents = agents.filter(agent => 
      agent.name.includes(query) || 
      agent.model.includes(query) || 
      agent.status.includes(query)
    );
    agentsList.innerHTML = "";

    filteredAgents.forEach(agent => {
      const agentItem = document.createElement("div");
      agentItem.className = "agent-item";
      agentItem.innerHTML = `
        <h3>${agent.name}</h3>
        <p>Status: ${agent.status}</p>
        <p>Configuration: ${JSON.stringify(agent.configuration)}</p>
        <p>Recent Activities: ${agent.recentActivities.join(", ")}</p>
        <button class="edit-agent" data-name="${agent.name}">Edit</button>
        <button class="clone-agent" data-name="${agent.name}">Clone</button>
        <button class="delete-agent" data-name="${agent.name}">Delete</button>
      `;
      agentsList.appendChild(agentItem);
    });
  }

  function handleAgentActions(event) {
    const target = event.target;
    const agentName = target.getAttribute("data-name");

    if (target.classList.contains("edit-agent")) {
      // Handle edit agent
    } else if (target.classList.contains("clone-agent")) {
      const agent = getAgent(agentName);
      createAgent({ ...agent, name: `${agent.name}-clone` });
      displayAgents();
    } else if (target.classList.contains("delete-agent")) {
      // Handle delete agent
    }
  }

  document.getElementById("search-agent").addEventListener("input", (event) => {
    searchAgents(event.target.value);
  });

  agentsList.addEventListener("click", handleAgentActions);

  displayAgents();
});
