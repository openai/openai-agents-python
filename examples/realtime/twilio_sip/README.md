# Twilio SIP Realtime Example

This example shows how to handle OpenAI Realtime SIP calls with the Agents SDK. Incoming calls are accepted through the Realtime Calls API, a triage agent answers with a fixed greeting, and handoffs route the caller to specialist agents (FAQ lookup and record updates) similar to the realtime UI demo.

## Prerequisites

- Python 3.9+
- An OpenAI API key with Realtime API access
- A configured webhook secret for your OpenAI project
- A Twilio account with a phone number and Elastic SIP Trunking enabled
- A public HTTPS endpoint for local development (for example, [ngrok](https://ngrok.com/))

## Configure OpenAI

1. In [platform settings](https://platform.openai.com/settings) select your project.
2. Create a webhook pointing to `https://<your-public-host>/openai/webhook` with "realtime.call.incoming" event type and note the signing secret. The example verifies each webhook with `OPENAI_WEBHOOK_SECRET`.

## Configure Twilio Elastic SIP Trunking

1. Create (or edit) an Elastic SIP trunk.
2. On the **Origination** tab, add an origination SIP URI of `sip:proj_<your_project_id>@sip.api.openai.com;transport=tls` so Twilio sends inbound calls to OpenAI. (The Termination tab always ends with `.pstn.twilio.com`, so leave it unchanged.)
3. Add at least one phone number to the trunk so inbound calls are forwarded to OpenAI.

## Setup

1. Install dependencies:
   ```bash
   uv pip install -r examples/realtime/twilio_sip/requirements.txt
   ```
2. Export required environment variables:
   ```bash
   export OPENAI_API_KEY="sk-..."
   export OPENAI_WEBHOOK_SECRET="whsec_..."
   ```
3. (Optional) Adjust the multi-agent logic in `examples/realtime/twilio_sip/agents.py` if you want
   to change the specialist agents or tools.
4. Run the FastAPI server:
   ```bash
   uv run uvicorn examples.realtime.twilio_sip.server:app --host 0.0.0.0 --port 8000
   ```
5. Expose the server publicly (example with ngrok):
   ```bash
   ngrok http 8000
   ```

## Test a Call

1. Place a call to the Twilio number attached to the SIP trunk.
2. Twilio sends the call to `sip.api.openai.com`; OpenAI fires `realtime.call.incoming`, which this example accepts.
3. The triage agent greets the caller, then either keeps the conversation or hands off to:
   - **FAQ Agent** – answers common questions via `faq_lookup_tool`.
   - **Records Agent** – writes short notes using `update_customer_record`.
4. The background task attaches to the call and logs transcripts plus basic events in the console.

You can edit `server.py` to change instructions, add tools, or integrate with internal systems once the SIP session is active.
