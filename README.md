# WhatsApp Realtime Chatbot

A real-time WhatsApp chatbot built on the official **WhatsApp Cloud API** (Meta),
with a pluggable LLM backend and per-user conversation memory.

Inbound messages arrive over a webhook, get answered by an LLM, and the reply is
sent back over the Graph API — typically within a second or two.

```
WhatsApp user  ──▶  Meta Cloud API  ──▶  POST /webhook  ──▶  Bot ──▶ LLM
                                              │                        │
                                              └── 200 OK immediately   │
                                                                       ▼
WhatsApp user  ◀──  Meta Cloud API  ◀─────────  Graph API send  ◀──────┘
```

## Why the Cloud API

There are unofficial libraries (`whatsapp-web.js`, Baileys) that log in by
scanning a QR code with a personal number. They are quicker to demo but violate
WhatsApp's Terms of Service and can get a number banned. This project uses the
official API: no ban risk, supports any number of users, and needs no phone
running.

## Features

- **Real-time replies** — webhook is acknowledged immediately, then the LLM call
  and send happen in a background task, so Meta never times out and retries.
- **Duplicate protection** — Meta redelivers webhooks it thinks failed. Every
  message ID is recorded, so a redelivery never produces a second reply.
- **Signature verification** — inbound payloads are validated against
  `X-Hub-Signature-256` using your app secret. Forged requests are rejected 403.
- **Conversation memory** — per-user history in SQLite, with a configurable
  context window and a `/reset` command.
- **Swappable LLM** — OpenAI-compatible (OpenAI, Azure, OpenRouter, vLLM),
  Anthropic, or an offline `echo` provider for testing with no API key.
- **Safety valves** — an optional sender allowlist, reply truncation to fit
  WhatsApp's 4096-character limit, and a graceful fallback message if the LLM
  errors out.

## Quick start (no credentials needed)

Runs with an offline echo provider so you can verify the plumbing first.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults work out of the box: LLM_PROVIDER=echo, SKIP_SIGNATURE_CHECK=0
python -m app                 # serves on http://localhost:8000
```

Check it is alive:

```bash
curl -s localhost:8000/health
# {"status":"ok","llm_provider":"echo","whatsapp_configured":false,...}
```

Run the tests:

```bash
pytest -q     # 22 tests
```

## Connecting to real WhatsApp

1. Create a Meta app at <https://developers.facebook.com> and add the
   **WhatsApp** product. You get a test number and a temporary token for free.
2. Copy the values into `.env`:
   - `WHATSAPP_TOKEN` — API Setup page (use a **permanent** token for production,
     not the 24-hour test token).
   - `WHATSAPP_PHONE_NUMBER_ID` — API Setup page.
   - `WHATSAPP_VERIFY_TOKEN` — any random string you invent; you type the same
     one into Meta's webhook config.
   - `WHATSAPP_APP_SECRET` — App Settings → Basic. Required for signature checks.
3. Expose your local server. Meta must reach a public HTTPS URL:
   ```bash
   ngrok http 8000        # or cloudflared tunnel --url http://localhost:8000
   ```
   The container has hosts on ports 12000/12001 if you are running there:
   `https://work-1-whqgwbnchxestfmr.prod-runtime.all-hands.dev/`
4. In Meta → WhatsApp → Configuration → Webhook:
   - Callback URL: `https://<your-public-host>/webhook`
   - Verify token: the same `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to the **messages** field.
5. Send a WhatsApp message to your test number. You should get a reply, and see
   the exchange in your terminal logs.

### Switching to a real LLM

```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...
# LLM_BASE_URL=https://openrouter.ai/api/v1   # optional, for compatible providers
```

Or `LLM_PROVIDER=anthropic` with `LLM_MODEL=claude-3-5-haiku-latest`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `WHATSAPP_TOKEN` | – | Graph API access token |
| `WHATSAPP_PHONE_NUMBER_ID` | – | Sending number's ID |
| `WHATSAPP_VERIFY_TOKEN` | – | Shared secret for the webhook handshake |
| `WHATSAPP_APP_SECRET` | – | Verifies inbound signatures |
| `GRAPH_API_VERSION` | `v21.0` | Graph API version |
| `GRAPH_BASE_URL` | `https://graph.facebook.com` | Override for proxies/self-hosted gateways |
| `LLM_PROVIDER` | `echo` | `openai`, `anthropic`, or `echo` |
| `LLM_MODEL` | `gpt-4o-mini` | Model name |
| `LLM_API_KEY` | – | Provider key |
| `LLM_BASE_URL` | – | OpenAI-compatible base URL override |
| `SYSTEM_PROMPT` | helpful assistant | Bot persona |
| `HISTORY_TURNS` | `10` | Turns of context sent to the LLM |
| `MAX_REPLY_CHARS` | `3500` | Reply truncation limit |
| `ALLOWED_NUMBERS` | empty | Comma-separated allowlist; empty = everyone |
| `SKIP_SIGNATURE_CHECK` | `0` | `1` accepts unsigned webhooks (**local testing only**) |
| `DATABASE_PATH` | `chatbot.db` | SQLite file |
| `PORT` | `8000` | Listen port |

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/webhook` | Meta's `hub.challenge` verification handshake |
| `POST` | `/webhook` | Inbound messages and delivery statuses |
| `GET` | `/health` | Liveness plus LLM/WhatsApp config status |
| `GET` | `/` | Service metadata |

## Project layout

```
app/
  config.py     Environment-driven settings
  whatsapp.py   Payload parsing, signature verification, Graph API sender
  llm.py        LLM providers behind one interface
  store.py      SQLite history and webhook idempotency
  main.py       FastAPI app, webhook routes, Bot orchestration
tests/
  test_chatbot.py        Parsing, store, bot logic, HTTP endpoints
  test_whatsapp_send.py  Real outbound HTTP against a local mock Graph server
```

## Adding a provider

Subclass `LLMProvider` in `app/llm.py`, implement
`async def reply(self, system, history) -> str`, and register it in
`build_provider`.

## Production notes

- Run behind a real ASGI server and process manager, e.g.
  `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`
  (or Gunicorn with `UvicornWorker`).
- SQLite is fine for a single worker. Switch `store.py` to Postgres if you scale
  horizontally — the interface is small.
- Terminate TLS at your proxy and keep `SKIP_SIGNATURE_CHECK=0`.
- Run `store.prune_processed()` periodically to bound the idempotency table.
- WhatsApp's free tier allows a limited number of conversations per month;
  check Meta's current pricing before going live.