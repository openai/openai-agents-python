class PCMRecorderProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.chunkSize = 4096;
        this.buffer = new Int16Array(this.chunkSize);
        this.offset = 0;
        this.pendingFrames = 0;
        this.maxPendingFrames = 10;
    }

    flushBuffer() {
        if (this.offset === 0) {
            return;
        }

        const chunk = new Int16Array(this.offset);
        chunk.set(this.buffer.subarray(0, this.offset));
        this.port.postMessage(chunk, [chunk.buffer]);

        this.offset = 0;
        this.pendingFrames = 0;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0) {
            return true;
        }

        const channel = input[0];
        if (!channel || channel.length === 0) {
            return true;
        }

        for (let i = 0; i < channel.length; i++) {
            let sample = channel[i];
            sample = Math.max(-1, Math.min(1, sample));
            this.buffer[this.offset++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

            if (this.offset === this.chunkSize) {
                this.flushBuffer();
            }
        }

        if (this.offset > 0) {
            this.pendingFrames += 1;
            if (this.pendingFrames >= this.maxPendingFrames) {
                this.flushBuffer();
            }
        }

        return true;
    }
}

registerProcessor('pcm-recorder', PCMRecorderProcessor);
