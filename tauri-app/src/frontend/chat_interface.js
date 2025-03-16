import { runAgent } from './agent_management';

const chatContainer = document.getElementById('chat-container');
const chatInput = document.getElementById('chat-input');
const chatForm = document.getElementById('chat-form');

chatForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  const userMessage = chatInput.value;
  appendMessage('User', userMessage);

  try {
    const agentResponse = await runAgent('chat-agent', userMessage);
    appendMessage('Agent', agentResponse);
  } catch (error) {
    console.error('Error running agent:', error);
    appendMessage('Error', 'Failed to get response from agent.');
  }

  chatInput.value = '';
});

function appendMessage(sender, message) {
  const messageElement = document.createElement('div');
  messageElement.classList.add('message', sender.toLowerCase());
  messageElement.innerText = `${sender}: ${message}`;
  chatContainer.appendChild(messageElement);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}
