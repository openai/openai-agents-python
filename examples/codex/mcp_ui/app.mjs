import { App } from '@modelcontextprotocol/ext-apps';
const app = new App({ name: 'Synthetic counter', version: '1.0.0' });
const count = document.getElementById('count');
const button = document.getElementById('increment');
const show = (result) => {
  count.textContent = String(result.structuredContent.count);
};
app.ontoolresult = show;
button.onclick = async () => {
  button.disabled = true;
  try {
    show(
      await app.callServerTool({ name: 'increment', arguments: { amount: 1 } }),
    );
  } catch (error) {
    document.getElementById('error').textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
await app.connect();
