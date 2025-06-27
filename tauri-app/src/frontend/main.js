const { invoke } = window.__TAURI__.tauri;

document.addEventListener("DOMContentLoaded", () => {
  const createAgentForm = document.getElementById("create-agent-form");

  createAgentForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const agentName = document.getElementById("agent-name").value;
    const agentInstructions = document.getElementById("agent-instructions").value;
    const agentModel = document.getElementById("agent-model").value;
    const agentTools = document.getElementById("agent-tools").value;
    const agentContext = document.getElementById("agent-context").value;
    const agentOutputTypes = document.getElementById("agent-output-types").value;
    const agentHandoffs = document.getElementById("agent-handoffs").value;
    const agentGuardrails = document.getElementById("agent-guardrails").value;
    const agentCloning = document.getElementById("agent-cloning").value;

    const agentData = {
      name: agentName,
      instructions: agentInstructions,
      model: agentModel,
      tools: agentTools,
      context: agentContext,
      outputTypes: agentOutputTypes,
      handoffs: agentHandoffs,
      guardrails: agentGuardrails,
      cloning: agentCloning,
    };

    try {
      await invoke("create_agent", { agentData });
      alert("Agent created successfully!");
    } catch (error) {
      console.error("Error creating agent:", error);
      alert("Failed to create agent.");
    }
  });
});
