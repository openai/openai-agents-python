import { emit, listen } from '@tauri-apps/api/event';

export function notifyAgentLifecycleEvent(eventType, agentName, details) {
  emit('agent-lifecycle-event', { eventType, agentName, details });
}

export function setupAgentLifecycleEventListener(callback) {
  listen('agent-lifecycle-event', (event) => {
    const { eventType, agentName, details } = event.payload;
    callback(eventType, agentName, details);
  });
}
