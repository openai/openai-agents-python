# Realtime Twilio Integration

This example demonstrates how to connect the OpenAI Realtime API to a phone call using Twilio's Media Streams. The server handles incoming phone calls and streams audio between Twilio and the OpenAI Realtime API, enabling real-time voice conversations with an AI agent over the phone.

## Prerequisites

-   Python 3.10+
-   OpenAI API key with [Realtime API](https://platform.openai.com/docs/guides/realtime) access
-   [Twilio](https://www.twilio.com/docs/voice) account with a phone number
-   A tunneling service like [ngrok](https://ngrok.com/) to expose your local server

## Setup

1. **Create an HTTPS tunnel to port 8000, e.g. via ngrok:**

    ```bash
    ngrok http 8000
    ```

    Note the public URL (e.g., `https://abc123.ngrok.io`)

2. **Configure and start the server:**

    Set `OPENAI_API_KEY` and `TWILIO_AUTH_TOKEN` through your secret-management mechanism. Use the Auth Token for the Twilio account receiving the calls, not its Account SID or an API key.

    ```bash
    export TWILIO_PUBLIC_BASE_URL="https://your-public-host.example"
    uv run --with-requirements requirements.txt server.py
    ```

    Set `TWILIO_PUBLIC_BASE_URL` to your actual tunnel's HTTPS origin, without a path, query, or credentials. The server refuses to start without the required Twilio configuration. Restart the server when the public URL or Auth Token changes.

3. **Configure your Twilio phone number:**
    - Log into your Twilio Console
    - Select your phone number
    - Set the webhook URL for incoming calls to: `https://your-ngrok-url.ngrok.io/incoming-call`
    - Set the HTTP method to POST

## Usage

1. Call your Twilio phone number
2. You'll hear: "Hello! You're now connected to an AI assistant. You can start talking!"
3. Start speaking - the AI will respond in real-time
4. The assistant has access to tools like weather information and current time

## How It Works

1. **Incoming Call**: When someone calls your Twilio number, Twilio makes a request to `/incoming-call`
2. **TwiML Response**: The server returns TwiML that:
    - Plays a greeting message
    - Connects the call to a WebSocket stream at `/media-stream`
3. **WebSocket Connection**: Twilio establishes a WebSocket connection for bidirectional audio streaming
4. **Call handler**: The `TwilioHandler` class owns the WebSocket message handling:
    - Takes ownership of the Twilio WebSocket after initial handshake
    - Runs its own message loop to process all Twilio messages
    - Handles protocol differences between Twilio and OpenAI
    - Automatically sets G.711 μ-law audio format for Twilio compatibility
    - Manages audio chunk tracking for interruption support
    - Closes the Realtime session and cancels and awaits call-owned tasks when the call ends
5. **Audio Processing**:
    - Audio from the caller is base64 decoded and sent to OpenAI Realtime API
    - Audio responses from OpenAI are base64 encoded and sent back to Twilio
    - Twilio plays the audio to the caller

## Configuration

-   **Port**: Set `PORT` environment variable (default: 8000)
-   **OpenAI API Key**: Set `OPENAI_API_KEY` environment variable
-   **Twilio Auth Token**: Set `TWILIO_AUTH_TOKEN` for request signature verification
-   **Public origin**: Set `TWILIO_PUBLIC_BASE_URL` to the externally visible HTTPS origin. This example serves its endpoints at the origin root, without a proxy path prefix.
-   **Agent Instructions**: Modify the `RealtimeAgent` configuration in `twilio_handler.py`
-   **Tools**: Add or modify function tools in `twilio_handler.py`

## Request authentication and deployment limits

The server verifies `X-Twilio-Signature` on both `/incoming-call` and the `/media-stream` WebSocket handshake before opening an OpenAI session. Verification and generated TwiML use the configured public origin, not `Host` or forwarding headers. TLS may terminate at your tunnel or reverse proxy; preserve request paths, query strings, form fields, and the signature header. Invalid or missing signatures are rejected.

The WebSocket signature covers the public `wss://` stream URL. The example also accepts the trailing-slash signature variant documented by Twilio. Stream URLs cannot contain query parameters; no shared secret is placed in the URL. See [Twilio request validation](https://www.twilio.com/docs/usage/security) and [Media Streams security](https://www.twilio.com/docs/global-infrastructure/firewall-configurations/media-streams-configuration).

Incoming POST webhook bodies are limited to 64 KiB while streaming, before signature verification. The form parser also limits requests to 100 fields and 8 KiB per encoded field (name and value combined). Requests exceeding these example-specific limits receive HTTP 413, regardless of the `Content-Length` header.

Each inbound WebSocket message is limited to 64 KiB as an example-specific policy. The included launcher applies that limit in Uvicorn before buffering the complete message, and the handler checks it before JSON parsing. If you use a different ASGI launcher, configure an equivalent WebSocket message-size limit there.

Signature validation authenticates Twilio requests; it does not authorize individual callers, prevent replay of a captured signed request, or limit legitimate call volume. Before a public deployment, configure caller authorization, call-duration and concurrency limits, request rate limits, and spending controls for your application. Keep Auth Tokens and signature headers out of logs. This example does not implement those deployment policies.

## Troubleshooting

-   **WebSocket connection issues**: Ensure your ngrok URL is correct and publicly accessible
-   **Audio quality**: Twilio streams audio in mulaw format at 8kHz, which may affect quality
-   **Latency**: Network latency between Twilio, your server, and OpenAI affects response time
-   **Logs**: Check the console output for detailed connection and error logs

## Architecture

```
Phone Call → Twilio → WebSocket → TwilioHandler → OpenAI Realtime API
                                              ↓
                                      RealtimeAgent with Tools
                                              ↓
                           Audio Response → Twilio → Phone Call
```

The `TwilioHandler` acts as a bridge between Twilio's Media Streams and OpenAI's Realtime API, handling the protocol differences and audio format conversions. It manages a Realtime session and its call-owned background tasks.
