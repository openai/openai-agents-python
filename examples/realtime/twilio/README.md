# Realtime Twilio Integration

This example demonstrates how to connect the OpenAI Realtime API to a phone call using Twilio's Media Streams. The server handles incoming phone calls and streams audio between Twilio and the OpenAI Realtime API, enabling real-time voice conversations with an AI agent over the phone.

## Prerequisites

-   Python 3.9+
-   OpenAI API key with [Realtime API](https://platform.openai.com/docs/guides/realtime) access
-   [Twilio](https://www.twilio.com/docs/voice) account with a phone number
-   A tunneling service like [ngrok](https://ngrok.com/) to expose your local server

## Setup

1. **Start the server:**

    ```bash
    uv run server.py
    ```

    The server will start on port 8000 by default.

2. **Expose the server publicly, e.g. via ngrok:**

    ```bash
    ngrok http 8000
    ```

    Note the public URL (e.g., `https://abc123.ngrok.io`)

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
4. **Transport Layer**: The `TwilioRealtimeTransportLayer` class owns the WebSocket message handling:
    - Takes ownership of the Twilio WebSocket after initial handshake
    - Runs its own message loop to process all Twilio messages
    - Handles protocol differences between Twilio and OpenAI
    - Automatically sets G.711 μ-law audio format for Twilio compatibility
    - Manages audio chunk tracking for interruption support
    - Wraps the OpenAI realtime model instead of subclassing it
5. **Audio Processing**:
    - Audio from the caller is base64 decoded and sent to OpenAI Realtime API
    - Audio responses from OpenAI are base64 encoded and sent back to Twilio
    - Twilio plays the audio to the caller

## Configuration

-   **Port**: Set `PORT` environment variable (default: 8000)
-   **OpenAI API Key**: Set `OPENAI_API_KEY` environment variable
-   **Agent Instructions**: Modify the `RealtimeAgent` configuration in `server.py`
-   **Tools**: Add or modify function tools in `server.py`

## Troubleshooting

-   **WebSocket connection issues**: Ensure your ngrok URL is correct and publicly accessible
-   **Audio quality**: Twilio streams audio in mulaw format at 8kHz, which may affect quality
-   **Latency**: Network latency between Twilio, your server, and OpenAI affects response time
-   **Logs**: Check the console output for detailed connection and error logs

## Architecture

```
Phone Call → Twilio → WebSocket → TwilioRealtimeTransportLayer → OpenAI Realtime API
                                              ↓
                                      RealtimeAgent with Tools
                                              ↓
                           Audio Response → Twilio → Phone Call
```

The `TwilioRealtimeTransportLayer` acts as a bridge between Twilio's Media Streams and OpenAI's Realtime API, handling the protocol differences and audio format conversions. It wraps the OpenAI realtime model to provide a clean interface for Twilio integration.
