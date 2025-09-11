class RealtimeDemo {
    constructor() {
        this.ws = null;
        this.isConnected = false;
        this.isMuted = false;
        this.isCapturing = false;
        this.audioContext = null;
        this.processor = null;
        this.stream = null;
        this.sessionId = this.generateSessionId();

        // Audio playback queue
        this.audioQueue = [];
        this.isPlayingAudio = false;
        this.playbackAudioContext = null;
        this.currentAudioSource = null;
        this.currentAudioGain = null; // per-chunk gain for smooth fades
        this.playbackFadeSec = 0.02; // ~20ms fade to reduce clicks
        this.messageNodes = new Map(); // item_id -> DOM node
        this.seenItemIds = new Set(); // item_id set for append-only syncing

        this.initializeElements();
        this.setupEventListeners();
    }

    initializeElements() {
        this.connectBtn = document.getElementById('connectBtn');
        this.muteBtn = document.getElementById('muteBtn');
        this.imageBtn = document.getElementById('imageBtn');
        this.imageInput = document.getElementById('imageInput');
        this.imagePrompt = document.getElementById('imagePrompt');
        this.status = document.getElementById('status');
        this.messagesContent = document.getElementById('messagesContent');
        this.eventsContent = document.getElementById('eventsContent');
        this.toolsContent = document.getElementById('toolsContent');
    }

    setupEventListeners() {
        this.connectBtn.addEventListener('click', () => {
            if (this.isConnected) {
                this.disconnect();
            } else {
                this.connect();
            }
        });

        this.muteBtn.addEventListener('click', () => {
            this.toggleMute();
        });

        // Image upload
        this.imageBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('Send Image clicked');
            // Programmatically open the hidden file input
            this.imageInput.click();
        });

        this.imageInput.addEventListener('change', async (e) => {
            console.log('Image input change fired');
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            await this._handlePickedFile(file);
            this.imageInput.value = '';
        });

        this._handlePickedFile = async (file) => {
            try {
                const dataUrl = await this.prepareDataURL(file);
                const promptText = (this.imagePrompt && this.imagePrompt.value) || '';
                // Send to server; server forwards to Realtime API.
                // Use chunked frames to avoid WS frame limits.
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    console.log('Interrupting and sending image (chunked) to server WebSocket');
                    // Stop any current audio locally and tell model to interrupt
                    this.stopAudioPlayback();
                    this.ws.send(JSON.stringify({ type: 'interrupt' }));
                    const id = 'img_' + Math.random().toString(36).slice(2);
                    const CHUNK = 60_000; // ~60KB per frame
                    this.ws.send(JSON.stringify({ type: 'image_start', id, text: promptText }));
                    for (let i = 0; i < dataUrl.length; i += CHUNK) {
                        const chunk = dataUrl.slice(i, i + CHUNK);
                        this.ws.send(JSON.stringify({ type: 'image_chunk', id, chunk }));
                    }
                    this.ws.send(JSON.stringify({ type: 'image_end', id }));
                } else {
                    console.warn('Not connected; image will not be sent. Click Connect first.');
                }
                // Add to UI immediately for better feedback
                console.log('Adding local user image bubble');
                this.addUserImageMessage(dataUrl, promptText);
            } catch (err) {
                console.error('Failed to process image:', err);
            }
        };
    }

    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9);
    }

    async connect() {
        try {
            this.ws = new WebSocket(`ws://localhost:8000/ws/${this.sessionId}`);

            this.ws.onopen = () => {
                this.isConnected = true;
                this.updateConnectionUI();
                this.startContinuousCapture();
            };

            this.ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                this.handleRealtimeEvent(data);
            };

            this.ws.onclose = () => {
                this.isConnected = false;
                this.updateConnectionUI();
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };

        } catch (error) {
            console.error('Failed to connect:', error);
        }
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
        }
        this.stopContinuousCapture();
    }

    updateConnectionUI() {
        if (this.isConnected) {
            this.connectBtn.textContent = 'Disconnect';
            this.connectBtn.className = 'connect-btn connected';
            this.status.textContent = 'Connected';
            this.status.className = 'status connected';
            this.muteBtn.disabled = false;
        } else {
            this.connectBtn.textContent = 'Connect';
            this.connectBtn.className = 'connect-btn disconnected';
            this.status.textContent = 'Disconnected';
            this.status.className = 'status disconnected';
            this.muteBtn.disabled = true;
        }
    }

    toggleMute() {
        this.isMuted = !this.isMuted;
        this.updateMuteUI();
    }

    updateMuteUI() {
        if (this.isMuted) {
            this.muteBtn.textContent = '🔇 Mic Off';
            this.muteBtn.className = 'mute-btn muted';
        } else {
            this.muteBtn.textContent = '🎤 Mic On';
            this.muteBtn.className = 'mute-btn unmuted';
            if (this.isCapturing) {
                this.muteBtn.classList.add('active');
            }
        }
    }

    readFileAsDataURL(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    async prepareDataURL(file) {
        const original = await this.readFileAsDataURL(file);
        try {
            const img = new Image();
            img.decoding = 'async';
            const loaded = new Promise((res, rej) => {
                img.onload = () => res();
                img.onerror = rej;
            });
            img.src = original;
            await loaded;

            const maxDim = 1024;
            const maxSide = Math.max(img.width, img.height);
            const scale = maxSide > maxDim ? (maxDim / maxSide) : 1;
            const w = Math.max(1, Math.round(img.width * scale));
            const h = Math.max(1, Math.round(img.height * scale));

            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);
            return canvas.toDataURL('image/jpeg', 0.85);
        } catch (e) {
            console.warn('Image resize failed; sending original', e);
            return original;
        }
    }

    async startContinuousCapture() {
        if (!this.isConnected || this.isCapturing) return;

        // Check if getUserMedia is available
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error('getUserMedia not available. Please use HTTPS or localhost.');
        }

        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 24000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true
                }
            });

            this.audioContext = new AudioContext({ sampleRate: 24000, latencyHint: 'interactive' });
            const source = this.audioContext.createMediaStreamSource(this.stream);

            // Create a script processor to capture audio data
            this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
            source.connect(this.processor);
            this.processor.connect(this.audioContext.destination);

            this.processor.onaudioprocess = (event) => {
                if (!this.isMuted && this.ws && this.ws.readyState === WebSocket.OPEN) {
                    const inputBuffer = event.inputBuffer.getChannelData(0);
                    const int16Buffer = new Int16Array(inputBuffer.length);

                    // Convert float32 to int16
                    for (let i = 0; i < inputBuffer.length; i++) {
                        int16Buffer[i] = Math.max(-32768, Math.min(32767, inputBuffer[i] * 32768));
                    }

                    this.ws.send(JSON.stringify({
                        type: 'audio',
                        data: Array.from(int16Buffer)
                    }));
                }
            };

            this.isCapturing = true;
            this.updateMuteUI();

        } catch (error) {
            console.error('Failed to start audio capture:', error);
        }
    }

    stopContinuousCapture() {
        if (!this.isCapturing) return;

        this.isCapturing = false;

        if (this.processor) {
            this.processor.disconnect();
            this.processor = null;
        }

        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }

        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }

        this.updateMuteUI();
    }

    handleRealtimeEvent(event) {
        // Add to raw events pane
        this.addRawEvent(event);

        // Add to tools panel if it's a tool or handoff event
        if (event.type === 'tool_start' || event.type === 'tool_end' || event.type === 'handoff') {
            this.addToolEvent(event);
        }

        // Handle specific event types
        switch (event.type) {
            case 'audio':
                this.playAudio(event.audio);
                break;
            case 'audio_interrupted':
                this.stopAudioPlayback();
                break;
            case 'input_audio_timeout_triggered':
                // Ask server to commit the input buffer to expedite model response
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'commit_audio' }));
                }
                break;
            case 'history_updated':
                this.syncMissingFromHistory(event.history);
                this.updateLastMessageFromHistory(event.history);
                break;
            case 'history_added':
                // Append just the new item without clearing the thread.
                if (event.item) {
                    this.addMessageFromItem(event.item);
                }
                break;
        }
    }
    updateLastMessageFromHistory(history) {
        if (!history || !Array.isArray(history) || history.length === 0) return;
        // Find the last message item in history
        let last = null;
        for (let i = history.length - 1; i >= 0; i--) {
            const it = history[i];
            if (it && it.type === 'message') { last = it; break; }
        }
        if (!last) return;
        const itemId = last.item_id;

        // Extract a text representation (for assistant transcript updates)
        let text = '';
        if (Array.isArray(last.content)) {
            for (const part of last.content) {
                if (!part || typeof part !== 'object') continue;
                if (part.type === 'text' && part.text) text += part.text;
                else if (part.type === 'input_text' && part.text) text += part.text;
                else if ((part.type === 'input_audio' || part.type === 'audio') && part.transcript) text += part.transcript;
            }
        }

        const node = this.messageNodes.get(itemId);
        if (!node) {
            // If we haven't rendered this item yet, append it now.
            this.addMessageFromItem(last);
            return;
        }

        // Update only the text content of the bubble, preserving any images already present.
        const bubble = node.querySelector('.message-bubble');
        if (bubble && text && text.trim()) {
            // If there's an <img>, keep it and only update the trailing caption/text node.
            const hasImg = !!bubble.querySelector('img');
            if (hasImg) {
                // Ensure there is a caption div after the image
                let cap = bubble.querySelector('.image-caption');
                if (!cap) {
                    cap = document.createElement('div');
                    cap.className = 'image-caption';
                    cap.style.marginTop = '0.5rem';
                    bubble.appendChild(cap);
                }
                cap.textContent = text.trim();
            } else {
                bubble.textContent = text.trim();
            }
            this.scrollToBottom();
        }
    }

    syncMissingFromHistory(history) {
        if (!history || !Array.isArray(history)) return;
        for (const item of history) {
            if (!item || item.type !== 'message') continue;
            const id = item.item_id;
            if (!id) continue;
            if (!this.seenItemIds.has(id)) {
                this.addMessageFromItem(item);
            }
        }
    }

    addMessageFromItem(item) {
        try {
            if (!item || item.type !== 'message') return;
            const role = item.role;
            let content = '';
            let imageUrls = [];

            if (Array.isArray(item.content)) {
                for (const contentPart of item.content) {
                    if (!contentPart || typeof contentPart !== 'object') continue;
                    if (contentPart.type === 'text' && contentPart.text) {
                        content += contentPart.text;
                    } else if (contentPart.type === 'input_text' && contentPart.text) {
                        content += contentPart.text;
                    } else if (contentPart.type === 'input_audio' && contentPart.transcript) {
                        content += contentPart.transcript;
                    } else if (contentPart.type === 'audio' && contentPart.transcript) {
                        content += contentPart.transcript;
                    } else if (contentPart.type === 'input_image') {
                        const url = contentPart.image_url || contentPart.url;
                        if (typeof url === 'string' && url) imageUrls.push(url);
                    }
                }
            }

            let node = null;
            if (imageUrls.length > 0) {
                for (const url of imageUrls) {
                    node = this.addImageMessage(role, url, content.trim());
                }
            } else if (content && content.trim()) {
                node = this.addMessage(role, content.trim());
            }
            if (node && item.item_id) {
                this.messageNodes.set(item.item_id, node);
                this.seenItemIds.add(item.item_id);
            }
        } catch (e) {
            console.error('Failed to add message from item:', e, item);
        }
    }

    addMessage(type, content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}`;

        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';
        bubbleDiv.textContent = content;

        messageDiv.appendChild(bubbleDiv);
        this.messagesContent.appendChild(messageDiv);
        this.scrollToBottom();

        return messageDiv;
    }

    addImageMessage(role, imageUrl, caption = '') {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';

        const img = document.createElement('img');
        img.src = imageUrl;
        img.alt = 'Uploaded image';
        img.style.maxWidth = '220px';
        img.style.borderRadius = '8px';
        img.style.display = 'block';

        bubbleDiv.appendChild(img);
        if (caption) {
            const cap = document.createElement('div');
            cap.textContent = caption;
            cap.style.marginTop = '0.5rem';
            bubbleDiv.appendChild(cap);
        }

        messageDiv.appendChild(bubbleDiv);
        this.messagesContent.appendChild(messageDiv);
        this.scrollToBottom();

        return messageDiv;
    }

    addUserImageMessage(imageUrl, caption = '') {
        return this.addImageMessage('user', imageUrl, caption);
    }

    addRawEvent(event) {
        const eventDiv = document.createElement('div');
        eventDiv.className = 'event';

        const headerDiv = document.createElement('div');
        headerDiv.className = 'event-header';
        headerDiv.innerHTML = `
            <span>${event.type}</span>
            <span>▼</span>
        `;

        const contentDiv = document.createElement('div');
        contentDiv.className = 'event-content collapsed';
        contentDiv.textContent = JSON.stringify(event, null, 2);

        headerDiv.addEventListener('click', () => {
            const isCollapsed = contentDiv.classList.contains('collapsed');
            contentDiv.classList.toggle('collapsed');
            headerDiv.querySelector('span:last-child').textContent = isCollapsed ? '▲' : '▼';
        });

        eventDiv.appendChild(headerDiv);
        eventDiv.appendChild(contentDiv);
        this.eventsContent.appendChild(eventDiv);

        // Auto-scroll events pane
        this.eventsContent.scrollTop = this.eventsContent.scrollHeight;
    }

    addToolEvent(event) {
        const eventDiv = document.createElement('div');
        eventDiv.className = 'event';

        let title = '';
        let description = '';
        let eventClass = '';

        if (event.type === 'handoff') {
            title = `🔄 Handoff`;
            description = `From ${event.from} to ${event.to}`;
            eventClass = 'handoff';
        } else if (event.type === 'tool_start') {
            title = `🔧 Tool Started`;
            description = `Running ${event.tool}`;
            eventClass = 'tool';
        } else if (event.type === 'tool_end') {
            title = `✅ Tool Completed`;
            description = `${event.tool}: ${event.output || 'No output'}`;
            eventClass = 'tool';
        }

        eventDiv.innerHTML = `
            <div class="event-header ${eventClass}">
                <div>
                    <div style="font-weight: 600; margin-bottom: 2px;">${title}</div>
                    <div style="font-size: 0.8rem; opacity: 0.8;">${description}</div>
                </div>
                <span style="font-size: 0.7rem; opacity: 0.6;">${new Date().toLocaleTimeString()}</span>
            </div>
        `;

        this.toolsContent.appendChild(eventDiv);

        // Auto-scroll tools pane
        this.toolsContent.scrollTop = this.toolsContent.scrollHeight;
    }

    async playAudio(audioBase64) {
        try {
            if (!audioBase64 || audioBase64.length === 0) {
                console.warn('Received empty audio data, skipping playback');
                return;
            }

            // Add to queue
            this.audioQueue.push(audioBase64);

            // Start processing queue if not already playing
            if (!this.isPlayingAudio) {
                this.processAudioQueue();
            }

        } catch (error) {
            console.error('Failed to play audio:', error);
        }
    }

    async processAudioQueue() {
        if (this.isPlayingAudio || this.audioQueue.length === 0) {
            return;
        }

        this.isPlayingAudio = true;

        // Initialize audio context if needed
        if (!this.playbackAudioContext) {
            this.playbackAudioContext = new AudioContext({ sampleRate: 24000, latencyHint: 'interactive' });
        }

        // Ensure context is running (autoplay policies can suspend it)
        if (this.playbackAudioContext.state === 'suspended') {
            try { await this.playbackAudioContext.resume(); } catch {}
        }

        while (this.audioQueue.length > 0) {
            const audioBase64 = this.audioQueue.shift();
            await this.playAudioChunk(audioBase64);
        }

        this.isPlayingAudio = false;
    }

    async playAudioChunk(audioBase64) {
        return new Promise((resolve, reject) => {
            try {
                // Decode base64 to ArrayBuffer
                const binaryString = atob(audioBase64);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }

                const int16Array = new Int16Array(bytes.buffer);

                if (int16Array.length === 0) {
                    console.warn('Audio chunk has no samples, skipping');
                    resolve();
                    return;
                }

                const float32Array = new Float32Array(int16Array.length);

                // Convert int16 to float32
                for (let i = 0; i < int16Array.length; i++) {
                    float32Array[i] = int16Array[i] / 32768.0;
                }

                const audioBuffer = this.playbackAudioContext.createBuffer(1, float32Array.length, 24000);
                audioBuffer.getChannelData(0).set(float32Array);

                const source = this.playbackAudioContext.createBufferSource();
                source.buffer = audioBuffer;

                // Per-chunk gain with short fade-in/out to avoid clicks
                const gainNode = this.playbackAudioContext.createGain();
                const now = this.playbackAudioContext.currentTime;
                const fade = Math.min(this.playbackFadeSec, Math.max(0.005, audioBuffer.duration / 8));
                try {
                    gainNode.gain.cancelScheduledValues(now);
                    gainNode.gain.setValueAtTime(0.0, now);
                    gainNode.gain.linearRampToValueAtTime(1.0, now + fade);
                    const endTime = now + audioBuffer.duration;
                    gainNode.gain.setValueAtTime(1.0, Math.max(now + fade, endTime - fade));
                    gainNode.gain.linearRampToValueAtTime(0.0001, endTime);
                } catch {}

                source.connect(gainNode);
                gainNode.connect(this.playbackAudioContext.destination);

                // Store references to allow smooth stop on interruption
                this.currentAudioSource = source;
                this.currentAudioGain = gainNode;

                source.onended = () => {
                    this.currentAudioSource = null;
                    this.currentAudioGain = null;
                    resolve();
                };
                source.start();

            } catch (error) {
                console.error('Failed to play audio chunk:', error);
                reject(error);
            }
        });
    }

    stopAudioPlayback() {
        console.log('Stopping audio playback due to interruption');

        // Smoothly ramp down before stopping to avoid clicks
        if (this.currentAudioSource && this.playbackAudioContext) {
            try {
                const now = this.playbackAudioContext.currentTime;
                const fade = Math.max(0.01, this.playbackFadeSec);
                if (this.currentAudioGain) {
                    try {
                        this.currentAudioGain.gain.cancelScheduledValues(now);
                        // Capture current value to ramp from it
                        const current = this.currentAudioGain.gain.value ?? 1.0;
                        this.currentAudioGain.gain.setValueAtTime(current, now);
                        this.currentAudioGain.gain.linearRampToValueAtTime(0.0001, now + fade);
                    } catch {}
                }
                // Stop after the fade completes
                setTimeout(() => {
                    try { this.currentAudioSource && this.currentAudioSource.stop(); } catch {}
                    this.currentAudioSource = null;
                    this.currentAudioGain = null;
                }, Math.ceil(fade * 1000));
            } catch (error) {
                console.error('Error stopping audio source:', error);
            }
        }

        // Clear the audio queue
        this.audioQueue = [];

        // Reset playback state
        this.isPlayingAudio = false;

        console.log('Audio playback stopped and queue cleared');
    }

    scrollToBottom() {
        this.messagesContent.scrollTop = this.messagesContent.scrollHeight;
    }
}

// Initialize the demo when the page loads
document.addEventListener('DOMContentLoaded', () => {
    new RealtimeDemo();
});
