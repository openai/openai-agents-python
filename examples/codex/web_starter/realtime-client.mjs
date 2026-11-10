// A real microphone/player client for the native Python server's WebRTC call.
// The second browser peer is a synthetic provider media endpoint.
async function run() {
    const audio = document.querySelector("audio");
    const microphone = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: false,
    });
    const client = new RTCPeerConnection();
    const provider = new RTCPeerConnection();
    const audioContext = new AudioContext();
    const oscillator = audioContext.createOscillator();
    const destination = audioContext.createMediaStreamDestination();
    oscillator.connect(destination);
    oscillator.start();
    provider.addTrack(
        destination.stream.getAudioTracks()[0],
        destination.stream,
    );
    client.addTrack(microphone.getAudioTracks()[0], microphone);
    const channel = client.createDataChannel("oai-events");
    let microphoneTrack;
    provider.ontrack = ({ track }) => {
        microphoneTrack = track;
    };
    client.ontrack = ({ streams }) => {
        audio.srcObject = streams[0];
        void audio.play();
    };
    async function until(check) {
        const deadline = Date.now() + 10_000;
        while (Date.now() < deadline) {
            if (await check()) return;
            await new Promise((resolve) => setTimeout(resolve, 20));
        }
        throw new Error("Browser media did not reach the expected state");
    }
    try {
        await audioContext.resume();
        await client.setLocalDescription(await client.createOffer());
        await provider.setRemoteDescription(client.localDescription);
        await provider.setLocalDescription(await provider.createAnswer());
        await until(() => provider.iceGatheringState === "complete");
        const response = await fetch("/exchange", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                offerSdp: client.localDescription.sdp,
                answerSdp: provider.localDescription.sdp,
            }),
        });
        if (!response.ok) throw new Error("Native call exchange failed");
        const accepted = await response.json();
        await client.setRemoteDescription({
            type: "answer",
            sdp: accepted.answerSdp,
        });
        await until(
            () =>
                client.connectionState === "connected" &&
                microphoneTrack?.readyState === "live",
        );
        let microphoneBytes = 0;
        await until(async () => {
            const stats = await provider.getStats();
            microphoneBytes = [...stats.values()]
                .filter(
                    (entry) =>
                        entry.type === "inbound-rtp" && entry.kind === "audio",
                )
                .reduce((total, entry) => total + entry.bytesReceived, 0);
            return microphoneBytes > 0;
        });
        await until(
            () =>
                !audio.paused &&
                audio.currentTime > 0 &&
                audio.srcObject?.getAudioTracks().length === 1,
        );
        const started = audio.currentTime;
        await until(() => audio.currentTime > started);
        const playbackTime = audio.currentTime;
        const closed = await fetch("/close", { method: "POST" });
        if (!closed.ok) throw new Error("Native call cleanup failed");
        return { microphoneBytes, playbackTime, closed: true };
    } finally {
        channel.close();
        client.close();
        provider.close();
        for (const track of microphone.getTracks()) track.stop();
        oscillator.stop();
        await audioContext.close();
        audio.srcObject = null;
    }
}
document.querySelector("button").onclick = () => {
    window.acceptance = run().then(
        (result) => ({ ok: true, result }),
        (error) => ({ ok: false, error: String(error) }),
    );
};
