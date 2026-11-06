import { DefaultChatTransport, readUIMessageStream } from 'ai';

const $ = id => document.getElementById(id);
let reader, receipt, user, rendered, generation = 0, loggingIn = false, sending = false;
const keyFor = owner => `web-starter:${owner}`;
const owner = () => ({generation, user, receipt});
const current = saved => saved.generation === generation && saved.user === user && saved.receipt === receipt;
function controls() {
  $('login').disabled = loggingIn;
  $('user').disabled = loggingIn;
  $('composer').querySelector('button').disabled = loggingIn || sending || !user;
  for (const id of ['disconnect', 'reconnect', 'stop']) $(id).disabled = loggingIn || !user;
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Web-Starter': '1' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
function text(id, value) { $(id).textContent = value; }
function render(state) {
  if (state.id !== receipt || (rendered && current(rendered) && state.revision < rendered.revision)) return;
  rendered = {...owner(), revision: state.revision};
  text('status', state.status + (state.resync_required ? ' — recovered application snapshot after observation gap' : ''));
  text('request', state.prompt); // Application-owned user message.
  text('card', state.card ? `${state.card.title}: ${state.card.row_count} rows\n${JSON.stringify(state.card.preview)}` : 'Report pending');
  $('approval').replaceChildren();
  if (state.approval) {
    text('approval', state.approval.label);
    const saved = owner(), token = state.approval.id;
    for (const [label, allow] of [['Approve', true], ['Deny', false]]) {
      const button = document.createElement('button'); button.textContent = label;
      button.onclick = () => act(async () => {
        if (current(saved)) await api(`/api/receipts/${saved.receipt}/reply`, {id: token, allow});
      });
      $('approval').append(button);
    }
  }
  text('final', state.final ? `${state.final.title}\n${state.final.summary}` : `Final result: ${state.status}`);
  if (state.artifact) {
    const link = document.createElement('a'); link.textContent = 'Download validated XML';
    link.href = `/api/objects/artifact/${state.artifact.id}`; $('final').append(document.createElement('br'), link);
  }
}
async function connect() {
  reader?.abort();
  const controller = new AbortController(); reader = controller;
  const saved = owner();
  const ownsReader = () => current(saved) && reader === controller && !controller.signal.aborted;
  if (!saved.receipt || !saved.user) return;
  try {
    const state = await api(`/api/receipts/${saved.receipt}`);
    if (!ownsReader()) return;
    render(state); // Authorized snapshot owns reload and gap recovery.
    const transport = new DefaultChatTransport({api: `/api/receipts/${saved.receipt}/stream`, headers: {'X-Web-Starter': '1'}});
    const stream = await transport.sendMessages({trigger: 'submit-message', chatId: saved.receipt,
      messages: [], messageId: undefined, abortSignal: controller.signal});
    for await (const message of readUIMessageStream({stream, terminateOnError: true})) {
      if (!ownsReader()) return;
      const part = message.parts.find(part => part.type === 'data-snapshot');
      if (part) render(part.data);
    }
  } catch (error) { if (ownsReader()) text('error', error.message); }
}
async function act(action) {
  const saved = owner();
  try { text('error', ''); await action(); }
  catch (error) { if (current(saved)) text('error', error.message); }
}
$('login').onclick = () => {
  if (loggingIn) return;
  const selected = $('user').value;
  generation++; reader?.abort(); reader = null; user = receipt = null; loggingIn = true;
  for (const id of ['request', 'card', 'approval', 'final', 'error']) text(id, '');
  text('status', 'Choosing user…'); controls();
  act(async () => {
    try {
      const login = await api('/api/login', {user: selected});
      user = login.user; receipt = localStorage.getItem(keyFor(user));
      text('mode', login.mode === 'live' ? 'Live model mode. Tools and records are synthetic.' : 'Scripted no-key mode. This checks integration, not model quality.');
      text('status', `Signed in as ${user}`);
    } finally { loggingIn = false; controls(); }
    await connect();
  });
};
$('composer').onsubmit = event => {
  event.preventDefault();
  if (!user || loggingIn || sending) return;
  const saved = owner(), storageKey = `${keyFor(saved.user)}:pending`;
  sending = true; controls();
  act(async () => {
    try {
      // A lost response retries the exact key+payload, never a fresh operation.
      let envelope = JSON.parse(localStorage.getItem(storageKey) || 'null');
      if (!envelope) { envelope = {key: crypto.randomUUID(), prompt: $('prompt').value}; localStorage.setItem(storageKey, JSON.stringify(envelope)); }
      const state = await api('/api/receipts', envelope);
      if (!current(saved)) return; // Keep that user's pending envelope for reconciliation.
      receipt = state.id; localStorage.setItem(keyFor(saved.user), receipt); localStorage.removeItem(storageKey);
      render(state); await connect();
    } finally { sending = false; controls(); }
  });
};
$('disconnect').onclick = () => { reader?.abort(); reader = null; text('status', 'Reader disconnected. Work continues.'); };
$('reconnect').onclick = () => act(connect);
$('stop').onclick = () => act(async () => {
  const saved = owner();
  if (!saved.receipt || !saved.user) return;
  const state = await api(`/api/receipts/${saved.receipt}/stop`, {});
  if (current(saved)) render(state);
});
controls();
