const start = document.querySelector("#start");
const stop = document.querySelector("#stop");
const mute = document.querySelector("#mute");
const audio = document.querySelector("#audio");
const status = document.querySelector("#status");
const activity = document.querySelector("#activity");
const user = document.querySelector("#user");
const assistant = document.querySelector("#assistant");
let active;

function cleanup(call) {
  clearTimeout(call.timeout);
  call.microphone?.getTracks().forEach((track) => track.stop());
  call.events?.close();
  call.peer?.close();
  call.socket?.close();
  if (active === call) {
    active = undefined;
    audio.srcObject = null;
    start.disabled = false;
    stop.disabled = true;
    mute.disabled = true;
    mute.textContent = "Mute microphone";
  }
}

function end(call) {
  if (active !== call || call.closing) return;
  clearTimeout(call.timeout);
  call.closing = true;
  stop.disabled = true;
  mute.disabled = true;
  call.microphone?.getTracks().forEach((track) => { track.enabled = false; });
  status.textContent = "Finishing conversation...";
  if (call.socket?.readyState === WebSocket.OPEN) {
    call.socket.send(JSON.stringify({ type: "close" }));
  }
  call.timeout = setTimeout(() => {
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

start.addEventListener("click", async () => {
  start.disabled = true;
  status.textContent = "Connecting...";
  user.textContent = "";
  assistant.textContent = "";
  activity.textContent = "";
  const call = { closing: false, finalized: false };
  active = call;
  try {
    call.microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
    call.timeout = setTimeout(() => { if (active === call) end(call); }, 45000);
    call.peer = new RTCPeerConnection();
    call.microphone.getTracks().forEach((track) => call.peer.addTrack(track, call.microphone));
    call.peer.addEventListener("track", (event) => {
      if (active !== call) return;
      audio.srcObject = new MediaStream([event.track]);
      audio.play().catch(() => {
        if (active !== call || call.closing) return;
        status.textContent = "Select play in the audio controls to hear the assistant.";
      });
    });
    call.events = call.peer.createDataChannel("oai-events");
    call.events.addEventListener("message", ({ data }) => {
      if (active !== call) return;
      const event = JSON.parse(data);
      if (event.type === "session.started") {
        if (call.closing) return;
        clearTimeout(call.timeout);
        status.textContent = "Connected. Ask about order A0042 or A0043.";
        stop.disabled = false;
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
    });
    call.socket.addEventListener("message", async ({ data }) => {
      if (active !== call) return;
      const event = JSON.parse(data);
      try {
        if (event.type === "answer") {
          await call.peer.setRemoteDescription({ type: "answer", sdp: event.sdp });
        } else if (event.type === "session.input_transcript.delta") {
          user.textContent += event.delta;
        } else if (event.type === "session.output_transcript.delta") {
          assistant.textContent += event.delta;
        } else if (event.type === "backend") {
          activity.textContent += `${event.status}${event.result ? ": " + event.result : ""}\n`;
        } else if (event.type === "closed") {
          call.finalized = true;
          status.textContent = `Conversation ended (${event.reason}). Voice usage: ${event.usage?.seconds ?? "unknown"} seconds.`;
          cleanup(call);
        } else if (event.type === "error") {
          call.error = event.message;
          status.textContent = event.message;
        }
      } catch {
        end(call);
      }
    });
    call.socket.addEventListener("close", () => {
      if (active !== call) return;
      if (!call.finalized) status.textContent = call.error ?? "Disconnected without confirmed final session usage.";
      cleanup(call);
    });
    call.socket.addEventListener("error", () => {
      if (active === call) {
        status.textContent = "Could not connect to the application server.";
        cleanup(call);
      }
    });
  } catch (error) {
    if (active === call) status.textContent = error.message;
    cleanup(call);
  }
});

stop.addEventListener("click", () => { if (active) end(active); });
mute.addEventListener("click", () => {
  if (!active || active.closing) return;
  const tracks = active.microphone.getAudioTracks();
  const enabled = !tracks[0].enabled;
  tracks.forEach((track) => { track.enabled = enabled; });
  mute.textContent = enabled ? "Mute microphone" : "Unmute microphone";
});
window.addEventListener("pagehide", () => { if (active) cleanup(active); });
