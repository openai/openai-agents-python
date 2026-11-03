// Exercise the public client transport and message reader on SDK-generated SSE.
import { DefaultChatTransport, readUIMessageStream } from 'ai';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const bytes = new TextEncoder().encode(JSON.parse(input).body);
const transport = new DefaultChatTransport({
  fetch: async () => new Response(new ReadableStream({
    start(controller) {
      // Split encoded Unicode as well as SSE frames, as a real network can.
      for (let offset = 0; offset < bytes.length; offset += 7) {
        controller.enqueue(bytes.slice(offset, offset + 7));
      }
      controller.close();
    },
  }), {
    headers: {
      'content-type': 'text/event-stream',
      'x-vercel-ai-ui-message-stream': 'v1',
    },
  }),
});
const stream = await transport.sendMessages({
  trigger: 'submit-message',
  chatId: 'synthetic-chat',
  messageId: undefined,
  messages: [],
  abortSignal: undefined,
});
const [raw, parsed] = stream.tee();
const chunks = [];
const readingChunks = (async () => {
  for await (const chunk of raw) chunks.push(chunk);
})();
const messages = [];
let error = null;
try {
  for await (const message of readUIMessageStream({ stream: parsed, terminateOnError: true })) {
    messages.push(message);
  }
} catch (failure) {
  error = failure.message;
}
await readingChunks;
process.stdout.write(JSON.stringify({ chunks, messages, error }));
