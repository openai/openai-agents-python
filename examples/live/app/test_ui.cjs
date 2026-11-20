const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

class Target {
  constructor() { this.listeners = {}; this.attributes = new Map(); this.disabled = false; this.textContent = ""; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
}
class Element extends Target {
  constructor() {
    super();
    this.children = [];
    this.dataset = {};
  }

  get lastElementChild() { return this.children.at(-1) ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
const elements = Object.fromEntries([
  "call-control", "mute", "audio", "play-audio", "audio-status", "status", "inference-panel", "inference",
  "conversation-log", "timeline", "debug-toggle", "debug-panel", "debug-events",
].map(id => [id, new Element()]));
const callControl = elements["call-control"];
callControl.dataset.state = "idle";
callControl.textContent = "Connect";
const playAudio = elements["play-audio"];
playAudio.hidden = true;
const debugToggle = elements["debug-toggle"];
debugToggle.checked = false;
const inferencePanel = elements["inference-panel"];
inferencePanel.dataset = { state: "idle" };
const conversationLog = elements["conversation-log"];
conversationLog.clientHeight = 100;
// Model growing rows without depending on browser layout or waiting on a timer.
Object.defineProperty(conversationLog, "scrollHeight", {
  get: () => Math.max(conversationLog.clientHeight, elements.timeline.children.reduce(
    (height, row) => height + 140 + row.lastElementChild.textContent.length, 0,
  )),
});
let resolvePlay;
let rejectPlay;
elements.audio.play = () => new Promise((resolve, reject) => {
  resolvePlay = resolve;
  rejectPlay = reject;
});
class Peer extends Target {
  constructor() { super(); this.iceGatheringState = "complete"; Peer.last = this; }
  addTrack() {}
  createDataChannel() { this.events = new Target(); this.events.close = () => {}; return this.events; }
  async createOffer() { return { sdp: "offer" }; }
  async setLocalDescription(offer) { this.localDescription = offer; }
  close() {}
}
class Socket extends Target {
  constructor() { super(); this.readyState = 1; Socket.last = this; }
  close() { this.readyState = 3; }
  send() {}
}
Socket.OPEN = 1;
const track = { stops: 0, stop() { this.stops++; }, enabled: true };
let clockMs = 0;
const context = vm.createContext({
  performance: { now: () => clockMs },
  document: { querySelector: selector => elements[selector.slice(1)], createElement: () => new Element() },
  navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }) } },
  RTCPeerConnection: Peer, WebSocket: Socket, MediaStream: class {}, URL,
  location: { protocol: "http:", host: "localhost:8000" },
  window: new Target(), setTimeout: () => 1, clearTimeout() {},
});
vm.runInContext(fs.readFileSync("examples/live/app/static/app.js", "utf8"), context);
// Control pending playback without microphone, browser, or API access.
test("late browser failures preserve finished and replacement calls", async () => {
  elements["debug-events"].textContent = "Connect to see session events.";
  debugToggle.checked = true;
  debugToggle.listeners.change();
  assert.equal(elements["debug-events"].textContent, "Connect to see session events.");
  debugToggle.checked = false;
  debugToggle.listeners.change();
  assert.equal(callControl.dataset.state, "idle");
  assert.equal(callControl.textContent, "Connect");
  assert.equal(callControl.disabled, false);
  const connecting = callControl.listeners.click();
  assert.equal(callControl.dataset.state, "connecting");
  assert.equal(callControl.textContent, "Connecting...");
  assert.equal(callControl.disabled, true);
  await connecting;
  const pendingPlayback = Peer.last.listeners.track({ track: {} });
  await Socket.last.listeners.message({ data: JSON.stringify({ type: "closed", reason: "close_requested", usage: { seconds: 1 } }) });
  const finalizedStatus = elements.status.textContent;
  assert.equal(finalizedStatus, "Conversation ended (close_requested). Voice usage: 1 seconds.");
  rejectPlay(new Error("Playback aborted during cleanup."));
  await pendingPlayback;
  assert.equal(elements.status.textContent, finalizedStatus);
  assert.equal(playAudio.hidden, true);
  assert.equal(elements["audio-status"].textContent, "");
  assert.equal(callControl.disabled, false);
  assert.equal(callControl.textContent, "Connect");
  assert.equal(callControl.dataset.state, "idle");
  assert.equal(inferencePanel.dataset.state, "idle");

  await callControl.listeners.click();
  const oldSocket = Socket.last;
  let rejectDescription;
  Peer.last.setRemoteDescription = () => new Promise((resolve, reject) => { rejectDescription = reject; });
  const pendingAnswer = oldSocket.listeners.message({ data: JSON.stringify({ type: "answer", sdp: "answer" }) });
  await oldSocket.listeners.message({ data: JSON.stringify({ type: "closed", reason: "close_requested", usage: { seconds: 1 } }) });
  await callControl.listeners.click();
  await Socket.last.listeners.message({ data: JSON.stringify({ type: "session.resumed" }) });
  assert.equal(inferencePanel.dataset.state, "active");
  assert.equal(callControl.dataset.state, "connecting");
  assert.equal(callControl.disabled, true);
  Peer.last.events.listeners.message({ data: JSON.stringify({ type: "session.started" }) });
  assert.equal(inferencePanel.dataset.state, "active");
  assert.equal(elements.inference.textContent, "Active.");
  const nextStatus = elements.status.textContent;
  const nextSocket = Socket.last;
  assert.equal(callControl.dataset.state, "connected");
  assert.equal(callControl.textContent, "Disconnect");
  assert.equal(callControl.disabled, false);
  assert.equal(elements.mute.disabled, false);
  rejectDescription(new Error("Remote description failed after cleanup."));
  await pendingAnswer;
  await oldSocket.listeners.message({ data: JSON.stringify({ type: "session.paused" }) });
  await oldSocket.listeners.message({ data: JSON.stringify({ type: "session.input_transcript.delta", delta: "Stale call." }) });
  assert.equal(elements.timeline.children.length, 0);
  assert.equal(elements.status.textContent, nextStatus);
  assert.equal(inferencePanel.dataset.state, "active");
  assert.equal(callControl.dataset.state, "connected");
  assert.equal(callControl.disabled, false);
  assert.equal(elements.mute.disabled, false);
  assert.equal(nextSocket.readyState, Socket.OPEN);
  await nextSocket.listeners.message({ data: JSON.stringify({ type: "closed", reason: "close_requested", usage: { seconds: 1 } }) });
});

test("assistant audio offers a play button only after playback fails", async () => {
  await callControl.listeners.click();
  Peer.last.events.listeners.message({ data: JSON.stringify({ type: "session.started" }) });
  const autoplay = Peer.last.listeners.track({ track: {} });
  assert.equal(playAudio.hidden, true);
  assert.ok(elements.audio.srcObject);

  rejectPlay(new Error("Autoplay blocked."));
  await autoplay;
  assert.equal(playAudio.hidden, false);
  assert.match(elements["audio-status"].textContent, /Select Play assistant audio/);

  const retry = playAudio.listeners.click();
  resolvePlay();
  await retry;
  assert.equal(playAudio.hidden, true);
  assert.equal(elements["audio-status"].textContent, "");
  assert.equal(callControl.dataset.state, "connected");

  await Socket.last.listeners.message({ data: JSON.stringify({ type: "closed", reason: "close_requested" }) });
  assert.equal(elements.audio.srcObject, null);
  assert.equal(playAudio.hidden, true);
});

test("web sources render as clickable links with safe text and destinations", async () => {
  await callControl.listeners.click();
  Peer.last.events.listeners.message({ data: JSON.stringify({ type: "session.started" }) });
  const socket = Socket.last;
  await socket.listeners.message({ data: JSON.stringify({
    type: "web_search_sources",
    sources: [
      { url: "https://example.com/news", title: "<b>Example news</b>" },
      { url: "javascript:alert(1)", title: "Invalid source" },
      { url: "https://fake:secret@example.com/", title: "Invalid credentials" },
      { url: "not a URL", title: "Invalid URL" },
    ],
  }) });
  const row = elements.timeline.lastElementChild;
  assert.equal(row.dataset.source, "web");
  assert.equal(row.children[0].textContent, "Web sources");
  assert.equal(row.lastElementChild.children.length, 1);
  const link = row.lastElementChild.lastElementChild.lastElementChild;
  assert.equal(link.href, "https://example.com/news");
  assert.equal(link.textContent, "<b>Example news</b>");
  assert.equal(link.rel, "noopener noreferrer");

  await socket.listeners.message({ data: JSON.stringify({ type: "closed", reason: "close_requested" }) });
});

test("standby shows inference state and preserves the call through speech and silence", async () => {
  await callControl.listeners.click();
  const peer = Peer.last;
  const socket = Socket.last;
  const stopsBeforeStandby = track.stops;
  const receive = event => socket.listeners.message({ data: JSON.stringify(event) });
  const timelineRows = () => elements.timeline.children.map(row => [
    row.dataset.source, row.children[0].textContent, row.children[1].textContent,
  ]);

  assert.equal(elements.inference.textContent, "");
  assert.equal(inferencePanel.dataset.state, "idle");
  peer.events.listeners.message({ data: JSON.stringify({ type: "session.started" }) });
  assert.equal(elements.inference.textContent, "Waiting for speech.");
  assert.equal(inferencePanel.dataset.state, "waiting");
  await receive({ type: "session.input_transcript.delta", delta: "Find order " });
  assert.equal(elements.inference.textContent, "Active.");
  assert.equal(inferencePanel.dataset.state, "active");
  await receive({ type: "session.resumed" });
  await receive({ type: "session.input_transcript.delta", delta: "A0042." });
  await receive({ type: "session.output_transcript.delta", delta: "One moment." });
  await receive({ type: "backend", status: "working" });
  await receive({ type: "backend", status: "result_submitted", result: "A0042 has shipped." });
  await receive({ type: "backend", status: "continued" });
  await receive({ type: "session.output_transcript.delta", delta: "It has " });
  await receive({ type: "session.output_transcript.delta", delta: "shipped." });
  await receive({ type: "session.paused" });
  assert.equal(elements.inference.textContent, "Standby. Speak to resume.");
  assert.equal(inferencePanel.dataset.state, "standby");
  assert.equal(callControl.disabled, false);
  assert.equal(elements.mute.disabled, false);
  assert.equal(track.enabled, true);
  assert.equal(track.stops, stopsBeforeStandby);
  assert.equal(socket.readyState, Socket.OPEN);

  await receive({ type: "session.resumed" });
  await receive({ type: "session.input_transcript.delta", delta: " Actually, A0043." });
  assert.equal(elements.inference.textContent, "Active.");
  assert.equal(inferencePanel.dataset.state, "active");
  await receive({ type: "backend", status: "working" });
  await receive({ type: "session.output_transcript.delta", delta: "It is processing." });
  const expectedTimeline = [
    ["user", "You", "Find order A0042."],
    ["assistant", "Assistant", "One moment."],
    ["specialist", "Specialist", "Looking up the order\nResult submitted: A0042 has shipped.\nGPT-Live continuation requested"],
    ["assistant", "Assistant", "It has shipped."],
    ["user", "You", " Actually, A0043."],
    ["specialist", "Specialist", "Looking up the order"],
    ["assistant", "Assistant", "It is processing."],
  ];
  assert.deepEqual(timelineRows(), expectedTimeline);

  elements.mute.listeners.click();
  assert.equal(track.enabled, false);
  assert.equal(elements.mute.getAttribute("aria-pressed"), "true");
  assert.equal(elements.mute.title, "Unmute microphone");
  assert.equal(elements.mute.textContent, "");

  elements.mute.listeners.click();
  assert.equal(track.enabled, true);
  assert.equal(elements.mute.getAttribute("aria-pressed"), "false");
  assert.equal(elements.mute.title, "Mute microphone");

  await callControl.listeners.click();
  assert.equal(callControl.dataset.state, "closing");
  assert.equal(callControl.textContent, "Disconnecting...");
  assert.equal(callControl.disabled, true);
  assert.equal(track.enabled, false);
  assert.equal(elements.mute.disabled, true);
  assert.equal(elements.mute.getAttribute("aria-pressed"), "true");
  await receive({ type: "session.paused" });
  assert.equal(elements.status.textContent, "Finishing conversation...");
  assert.equal(inferencePanel.dataset.state, "idle");
  assert.equal(elements.inference.textContent, "");
  await receive({
    type: "closed",
    reason: "close_requested",
    usage: { seconds: 5, active_seconds: 5, paused_seconds: 0 },
  });
  assert.equal(elements.status.textContent, "Conversation ended (close_requested). Voice usage: 5 seconds (5 active, 0 standby).");
  assert.equal(callControl.dataset.state, "idle");
  assert.equal(callControl.textContent, "Connect");
  assert.equal(callControl.disabled, false);
  assert.equal(track.enabled, false);
  assert.equal(elements.mute.getAttribute("aria-pressed"), "false");
  assert.equal(elements.mute.title, "Mute microphone");
  assert.equal(elements.mute.textContent, "");
  assert.equal(inferencePanel.dataset.state, "idle");
  assert.equal(elements.inference.textContent, "");
  assert.deepEqual(timelineRows(), expectedTimeline);

  await callControl.listeners.click();
  assert.deepEqual(timelineRows(), []);
  await Socket.last.listeners.message({ data: JSON.stringify({
    type: "closed", reason: "close_requested", usage: { seconds: 0 },
  }) });
});

test("conversation follows growth near the bottom and preserves scrollback", async () => {
  await callControl.listeners.click();
  const socket = Socket.last;
  const receive = event => socket.listeners.message({ data: JSON.stringify(event) });
  const bottom = () => conversationLog.scrollHeight - conversationLog.clientHeight;

  assert.equal(conversationLog.scrollTop, 0);
  await receive({ type: "session.input_transcript.delta", delta: "Find order " });
  assert.equal(conversationLog.scrollTop, bottom());

  conversationLog.scrollTop = 10;
  await receive({ type: "session.input_transcript.delta", delta: "A0042." });
  assert.equal(conversationLog.scrollTop, 10);
  await receive({ type: "session.output_transcript.delta", delta: "One " });
  assert.equal(conversationLog.scrollTop, 10);

  conversationLog.scrollTop = bottom() - 20;
  await receive({ type: "session.output_transcript.delta", delta: "moment." });
  assert.equal(conversationLog.scrollTop, bottom());
  await receive({ type: "backend", status: "working" });
  assert.equal(conversationLog.scrollTop, bottom());

  await receive({ type: "closed", reason: "close_requested", usage: { seconds: 1 } });
  assert.equal(conversationLog.scrollTop, bottom());
  await callControl.listeners.click();
  assert.equal(conversationLog.scrollTop, 0);
  assert.equal(elements.timeline.children.length, 0);
  await Socket.last.listeners.message({ data: JSON.stringify({
    type: "closed", reason: "close_requested", usage: { seconds: 0 },
  }) });
});

test("debug toggle reveals buffered diagnostics and preserves them after call close", async () => {
  const debugPanel = elements["debug-panel"];
  const debugEvents = elements["debug-events"];
  debugEvents.textContent = "Connect to see session events.";
  debugEvents.clientHeight = 100;
  debugEvents.scrollTop = 0;
  Object.defineProperty(debugEvents, "scrollHeight", {
    get: () => Math.max(debugEvents.clientHeight, debugEvents.textContent.split("\n").length * 20),
  });
  const bottom = () => debugEvents.scrollHeight - debugEvents.clientHeight;
  const toggle = open => {
    debugToggle.checked = open;
    debugToggle.listeners.change();
    assert.equal(debugPanel.hidden, !open);
  };

  toggle(false);
  clockMs = 10000;
  await callControl.listeners.click();
  const socket = Socket.last;
  const receive = async event => {
    clockMs += 100;
    await socket.listeners.message({ data: JSON.stringify(event) });
  };
  clockMs += 100;
  Peer.last.events.listeners.message({ data: JSON.stringify({ type: "session.started", session: { id: "private-session" } }) });
  await receive({ type: "session.input_transcript.delta", delta: "private speech" });
  await receive({ type: "session.input_transcript.delta", delta: "private correction" });
  await receive({ type: "session.paused", event_id: "event_pause", sideband_ms: 310 });
  await receive({
    type: "backend", status: "result_submitted", event_id: "event_tool",
    source_response_id: "resp_1", call_id: "call_42", result: "private specialist result",
  });
  await receive({
    type: "backend", status: "continued", event_id: "event_continue",
    delegation_id: "del_42", source_response_id: "resp_1",
  });
  await receive({
    type: "session.resumed", sideband_ms: 670,
    diagnostics: [
      {
        name: "response.created", client_event_id: "event_continue", delegation_id: "del_42",
        response_id: "resp_2", sideband_ms: 620, payload: "private response payload",
      },
      { name: "response.completed", response_id: "resp_2", sideband_ms: 660 },
      { name: "private unexpected event", sideband_ms: 665 },
    ],
  });

  assert.equal(debugEvents.textContent, "Connect to see session events.");
  toggle(true);
  assert.equal(debugEvents.scrollTop, bottom());
  assert.deepEqual(debugEvents.textContent.split("\n"), [
    "+0.000s Connect requested",
    "+0.100s Received session.started",
    "+0.200s Received session.input_transcript.delta (2 fragments, last +0.300s)",
    "+0.400s Received session.paused event_id=event_pause sideband_ms=310",
    "+0.500s Sent response.item.create event_id=event_tool source_response_id=resp_1 call_id=call_42",
    "+0.600s Sent response.create event_id=event_continue delegation_id=del_42 source_response_id=resp_1",
    "+0.700s Received response.created client_event_id=event_continue delegation_id=del_42 response_id=resp_2 sideband_ms=620",
    "+0.700s Received response.completed response_id=resp_2 sideband_ms=660",
    "+0.700s Received session.resumed sideband_ms=670",
  ]);
  debugEvents.scrollTop = 0;
  await receive({ type: "session.output_transcript.delta", delta: "private answer" });
  assert.equal(debugEvents.scrollTop, 0);
  debugEvents.scrollTop = bottom() - 12;
  await receive({ type: "session.paused", event_id: "event_pause_again", sideband_ms: 860 });
  assert.equal(debugEvents.scrollTop, bottom());

  const beforeClose = debugEvents.textContent;
  toggle(false);
  await receive({
    type: "error", message: "private application error",
    diagnostics: [{ name: "error", code: "tool_outputs_required", sideband_ms: 900, message: "private error", event_id: "sk-debug-secret" }],
  });
  await callControl.listeners.click();
  await receive({ type: "closed", reason: "close_requested", usage: { seconds: 2 } });
  assert.equal(debugEvents.textContent, beforeClose);

  toggle(true);
  assert.equal(debugEvents.scrollTop, bottom());
  assert.deepEqual(debugEvents.textContent.slice(beforeClose.length + 1).split("\n"), [
    "+1.000s Received error code=tool_outputs_required sideband_ms=900",
    "+1.000s Received application error",
    "+1.000s Disconnecting",
    "+1.000s Sent app close",
    "+1.100s Received session.closed",
  ]);
  assert.doesNotMatch(debugEvents.textContent, /private|sk-debug-secret/);

  await callControl.listeners.click();
  assert.equal(debugEvents.textContent, "+0.000s Connect requested");
  toggle(false);
  for (let index = 0; index < 205; index++) {
    await Socket.last.listeners.message({ data: JSON.stringify({ type: "session.resumed", event_id: `event_${index}` }) });
  }
  toggle(true);
  const entries = debugEvents.textContent.split("\n");
  assert.equal(entries.length, 200);
  assert.match(entries[0], /event_id=event_5$/);
  assert.match(entries.at(-1), /event_id=event_204$/);
  await Socket.last.listeners.message({ data: JSON.stringify({
    type: "closed", reason: "close_requested", usage: { seconds: 0 },
  }) });
});
