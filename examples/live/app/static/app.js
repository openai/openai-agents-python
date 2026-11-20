const callControl = document.querySelector("#call-control");
const mute = document.querySelector("#mute");
const audio = document.querySelector("#audio");
const playAudio = document.querySelector("#play-audio");
const audioStatus = document.querySelector("#audio-status");
const status = document.querySelector("#status");
const inferencePanel = document.querySelector("#inference-panel");
const inference = document.querySelector("#inference");
const conversationLog = document.querySelector("#conversation-log");
const timeline = document.querySelector("#timeline");
const debugToggle = document.querySelector("#debug-toggle");
const debugPanel = document.querySelector("#debug-panel");
const debugEvents = document.querySelector("#debug-events");
let active;

const callControlLabels = {
  idle: "Connect",
  connecting: "Connecting...",
  connected: "Disconnect",
  closing: "Disconnecting...",
};

const inferenceMessages = {
  idle: "",
  waiting: "Waiting for speech.",
  active: "Active.",
  standby: "Standby. Speak to resume.",
};

const timelineLabels = {
  user: "You",
  assistant: "Assistant",
  specialist: "Specialist",
  web: "Web sources",
};

const specialistMessages = {
  working: "Looking up the order",
  result_submitted: "Result submitted",
  continued: "GPT-Live continuation requested",
};

const debugCommands = {
  working: "Received specialist work started",
  result_submitted: "Sent response.item.create",
  continued: "Sent response.create",
};
const debugResponses = new Set(["session.delegation.created", "response.created", "response.completed", "response.failed", "response.incomplete", "error"]);
const debugFieldNames = ["event_id", "client_event_id", "delegation_id", "source_response_id", "response_id", "call_id", "code"];
const debugTrace = [];

function renderDebug(jumpToLatest = false) {
  if (debugPanel.hidden || !debugTrace.length) return;
  const distanceFromBottom = debugEvents.scrollHeight - debugEvents.clientHeight - debugEvents.scrollTop;
  debugEvents.textContent = debugTrace.map(({ elapsed, last, count, label, fields }) => {
    const timestamp = `+${(elapsed / 1000).toFixed(3)}s`;
    const fragments = count > 1 ? ` (${count} fragments, last +${(last / 1000).toFixed(3)}s)` : "";
    return [timestamp, label + fragments, ...fields].join(" ");
  }).join("\n");
  if (jumpToLatest || distanceFromBottom <= 24) {
    debugEvents.scrollTop = debugEvents.scrollHeight - debugEvents.clientHeight;
  }
}

function traceDebug(call, label, event, coalesce = false) {
  if (active !== call) return;
  const elapsed = performance.now() - call.startedAt;
  const previous = debugTrace.at(-1);
  if (coalesce && previous?.label === label) {
    previous.count += 1;
    previous.last = elapsed;
  } else {
    const fields = debugFieldNames.flatMap(name => {
      const value = event?.[name];
      return typeof value === "string" && /^[a-zA-Z0-9_.-]{1,128}$/.test(value) && !/^sk[-_]/i.test(value)
        ? [`${name}=${value}`]
        : [];
    });
    if (Number.isSafeInteger(event?.sideband_ms) && event.sideband_ms >= 0) {
      fields.push(`sideband_ms=${event.sideband_ms}`);
    }
    debugTrace.push({ elapsed, last: elapsed, count: 1, label, fields });
    if (debugTrace.length > 200) debugTrace.shift();
  }
  renderDebug();
}

function updateDebugPanel() {
  debugPanel.hidden = !debugToggle.checked;
  renderDebug(true);
}

debugToggle.addEventListener("change", updateDebugPanel);
updateDebugPanel();

function appendTimeline(source, content) {
  const distanceFromBottom = conversationLog.scrollHeight - conversationLog.clientHeight - conversationLog.scrollTop;
  const previous = timeline.lastElementChild;
  const isText = typeof content === "string";
  if (isText && previous?.dataset.source === source) {
    const separator = source === "specialist" ? "\n" : "";
    previous.lastElementChild.textContent += separator + content;
  } else {
    const entry = document.createElement("li");
    entry.dataset.source = source;
    const label = document.createElement("h3");
    label.textContent = timelineLabels[source];
    const body = isText ? document.createElement("pre") : content;
    if (isText) body.textContent = content;
    entry.append(label, body);
    timeline.append(entry);
  }

  if (distanceFromBottom <= 24) {
    conversationLog.scrollTop = conversationLog.scrollHeight - conversationLog.clientHeight;
  }
}

function appendWebSources(sources) {
  if (!Array.isArray(sources)) return;
  const list = document.createElement("ul");
  list.className = "web-sources";
  for (const source of sources) {
    let url;
    try {
      url = new URL(source.url);
    } catch {
      continue;
    }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) continue;
    const link = document.createElement("a");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = typeof source.title === "string" && source.title ? source.title : url.hostname;
    const item = document.createElement("li");
    item.append(link);
    list.append(item);
  }
  if (list.lastElementChild) appendTimeline("web", list);
}

function setInference(state) {
  inferencePanel.dataset.state = state;
  inference.textContent = inferenceMessages[state];
}

function setCallControl(state) {
  callControl.dataset.state = state;
  callControl.textContent = callControlLabels[state];
  callControl.disabled = state === "connecting" || state === "closing";
}

function renderMuteState(muted) {
  mute.setAttribute("aria-pressed", String(muted));
  mute.title = muted ? "Unmute microphone" : "Mute microphone";
}

function showAudioRetry(visible) {
  playAudio.hidden = !visible;
  audioStatus.textContent = visible ? "Assistant audio did not start. Select Play assistant audio to try again." : "";
}

async function startAssistantAudio(call) {
  showAudioRetry(false);
  try {
    await audio.play();
    if (!call.closing) traceDebug(call, "Audio play accepted");
  } catch {
    if (active !== call || call.closing) return;
    traceDebug(call, "Audio play blocked or failed");
    showAudioRetry(true);
  }
}

function cleanup(call) {
  clearTimeout(call.timeout);
  call.microphone?.getTracks().forEach((track) => track.stop());
  call.events?.close();
  call.peer?.close();
  call.socket?.close();
  if (active === call) {
    active = undefined;
    audio.srcObject = null;
    showAudioRetry(false);
    setInference("idle");
    setCallControl("idle");
    mute.disabled = true;
    renderMuteState(false);
  }
}

function end(call) {
  if (active !== call || call.closing) return;
  clearTimeout(call.timeout);
  call.closing = true;
  traceDebug(call, "Disconnecting");
  setCallControl("closing");
  mute.disabled = true;
  showAudioRetry(false);
  call.microphone?.getTracks().forEach((track) => { track.enabled = false; });
  renderMuteState(true);
  status.textContent = "Finishing conversation...";
  setInference("idle");
  if (call.socket?.readyState === WebSocket.OPEN) {
    call.socket.send(JSON.stringify({ type: "close" }));
    traceDebug(call, "Sent app close");
  }
  call.timeout = setTimeout(() => {
    traceDebug(call, "Disconnect timed out");
    status.textContent = "Disconnected without confirmed final session usage.";
    cleanup(call);
  }, 35000);
}

async function gatherIce(peer) {
  if (peer.iceGatheringState === "complete") return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      peer.removeEventListener("icegatheringstatechange", changed);
      reject(new Error("ICE gathering timed out."));
    }, 10000);
    function changed() {
      if (peer.iceGatheringState === "complete") {
        clearTimeout(timeout);
        peer.removeEventListener("icegatheringstatechange", changed);
        resolve();
      }
    }
    peer.addEventListener("icegatheringstatechange", changed);
    changed();
  });
}

callControl.addEventListener("click", async () => {
  if (active) {
    end(active);
    return;
  }
  setCallControl("connecting");
  status.textContent = "Connecting...";
  timeline.replaceChildren();
  conversationLog.scrollTop = 0;
  debugTrace.length = 0;
  const call = { closing: false, finalized: false, startedAt: performance.now() };
  active = call;
  traceDebug(call, "Connect requested");
  try {
    call.microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
    call.timeout = setTimeout(() => { if (active === call) end(call); }, 45000);
    call.peer = new RTCPeerConnection();
    call.microphone.getTracks().forEach((track) => call.peer.addTrack(track, call.microphone));
    call.peer.addEventListener("track", (event) => {
      if (active !== call || call.closing) return;
      audio.srcObject = new MediaStream([event.track]);
      return startAssistantAudio(call);
    });
    call.events = call.peer.createDataChannel("oai-events");
    call.events.addEventListener("message", ({ data }) => {
      if (active !== call) return;
      const event = JSON.parse(data);
      if (event.type === "session.started") {
        traceDebug(call, "Received session.started");
        if (call.closing) return;
        clearTimeout(call.timeout);
        status.textContent = "Connected.";
        if (inferencePanel.dataset.state === "idle") setInference("waiting");
        setCallControl("connected");
        mute.disabled = false;
      }
      // The server is the only function executor; this channel never runs tools.
    });
    call.peer.addEventListener("connectionstatechange", () => {
      if (active === call && ["failed", "disconnected"].includes(call.peer.connectionState)) {
        end(call);
      }
    });
    await call.peer.setLocalDescription(await call.peer.createOffer());
    await gatherIce(call.peer);
    if (active !== call || call.closing) {
      cleanup(call);
      return;
    }
    call.socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
    call.socket.addEventListener("open", () => {
      call.socket.send(JSON.stringify({ sdp: call.peer.localDescription.sdp }));
      traceDebug(call, "Sent WebRTC offer");
    });
    call.socket.addEventListener("message", async ({ data }) => {
      if (active !== call) return;
      const event = JSON.parse(data);
      try {
        for (const diagnostic of event.diagnostics ?? []) {
          if (debugResponses.has(diagnostic.name)) {
            traceDebug(call, `Received ${diagnostic.name}`, diagnostic);
          }
        }
        if (event.type === "answer") {
          traceDebug(call, "Received WebRTC answer");
          await call.peer.setRemoteDescription({ type: "answer", sdp: event.sdp });
        } else if (event.type === "session.paused" || event.type === "session.resumed") {
          traceDebug(call, `Received ${event.type}`, event);
          if (!call.closing) {
            setInference(event.type === "session.paused" ? "standby" : "active");
          }
        } else if (event.type === "session.input_transcript.delta" || event.type === "session.output_transcript.delta") {
          if (event.delta) traceDebug(call, `Received ${event.type}`, undefined, true);
          const state = inferencePanel.dataset.state;
          if (event.delta && !call.closing && (state === "idle" || state === "waiting")) {
            setInference("active");
          }
          appendTimeline(event.type === "session.input_transcript.delta" ? "user" : "assistant", event.delta);
        } else if (event.type === "backend") {
          const debugLabel = debugCommands[event.status];
          if (typeof debugLabel === "string") traceDebug(call, debugLabel, event);
          const message = specialistMessages[event.status] ?? event.status;
          appendTimeline("specialist", event.result ? `${message}: ${event.result}` : message);
        } else if (event.type === "web_search_sources") {
          traceDebug(call, "Received web search sources");
          appendWebSources(event.sources);
        } else if (event.type === "closed") {
          traceDebug(call, "Received session.closed");
          call.finalized = true;
          const usage = event.usage;
          const breakdown = usage?.active_seconds != null && usage.paused_seconds != null
            ? ` (${usage.active_seconds} active, ${usage.paused_seconds} standby)`
            : "";
          status.textContent = `Conversation ended (${event.reason}). Voice usage: ${usage?.seconds ?? "unknown"} seconds${breakdown}.`;
          cleanup(call);
        } else if (event.type === "error") {
          traceDebug(call, "Received application error");
          call.error = event.message;
          status.textContent = event.message;
        }
      } catch {
        traceDebug(call, "Could not process application event");
        end(call);
      }
    });
    call.socket.addEventListener("close", () => {
      if (active !== call) return;
      traceDebug(call, "Application socket closed");
      if (!call.finalized) status.textContent = call.error ?? "Disconnected without confirmed final session usage.";
      cleanup(call);
    });
    call.socket.addEventListener("error", () => {
      if (active === call) {
        traceDebug(call, "Application socket failed");
        status.textContent = "Could not connect to the application server.";
        cleanup(call);
      }
    });
  } catch (error) {
    traceDebug(call, "Connection setup failed");
    if (active === call) status.textContent = error.message;
    cleanup(call);
  }
});

playAudio.addEventListener("click", () => {
  if (active && !active.closing) return startAssistantAudio(active);
});

mute.addEventListener("click", () => {
  if (!active || active.closing) return;
  const tracks = active.microphone.getAudioTracks();
  const enabled = !tracks[0].enabled;
  tracks.forEach((track) => { track.enabled = enabled; });
  renderMuteState(!enabled);
});
window.addEventListener("pagehide", () => { if (active) cleanup(active); });
