import { DefaultChatTransport, readUIMessageStream } from 'ai';
import {
  AppBridge,
  PostMessageTransport,
} from '@modelcontextprotocol/ext-apps/app-bridge';
const $ = (id) => document.getElementById(id);
let bridge;
window.addEventListener('pagehide', () => {
  void bridge?.close();
});
async function api(path, body, token) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-MCP-Example': '1',
      ...(token ? { 'X-View': token } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
$('start').onclick = async () => {
  $('start').disabled = true;
  try {
    const transport = new DefaultChatTransport({
      api: '/api/chat',
      headers: { 'X-MCP-Example': '1' },
    });
    const stream = await transport.sendMessages({
      trigger: 'submit-message',
      chatId: 'synthetic-user',
      messages: [],
      messageId: undefined,
    });
    for await (const message of readUIMessageStream({
      stream,
      terminateOnError: true,
    })) {
      $('text').textContent = message.parts
        .filter((p) => p.type === 'text')
        .map((p) => p.text)
        .join('');
      $('tools').textContent = JSON.stringify(
        message.parts.filter(
          (p) => p.type === 'dynamic-tool' || p.type.startsWith('tool-'),
        ),
        null,
        2,
      );
    }
    $('status').textContent = 'Complete';
    const { view, token } = await api('/api/view');
    const iframe = $('app');
    bridge = new AppBridge(
      null,
      { name: 'Local native host', version: '1.0.0' },
      { serverTools: {} },
    );
    // The transport checks event.source against this exact iframe window.
    bridge.oncalltool = (params) =>
      api(
        '/api/action',
        { name: params.name, arguments: params.arguments ?? {} },
        token,
      );
    bridge.oninitialized = async () => {
      await bridge.sendToolInput({ arguments: view.arguments });
      await bridge.sendToolResult(view.result);
    };
    iframe.hidden = false;
    await bridge.connect(
      new PostMessageTransport(iframe.contentWindow, iframe.contentWindow),
    );
    iframe.srcdoc = view.resource.text;
  } catch (error) {
    $('error').textContent = error.message;
  }
};
