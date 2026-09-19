"""FastAPI application exposing the WhatsApp webhook.

Flow: Meta posts an inbound message -> we ack 200 immediately (Meta retries on
slow responses, which would duplicate replies) -> a background task builds the
prompt, calls the LLM, and sends the reply back through the Graph API.
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings
from .llm import LLMError, LLMProvider, build_provider
from .store import ConversationStore
from .whatsapp import IncomingMessage, WhatsAppClient, parse_webhook, verify_signature

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("whatsapp_bot")

RESET_COMMANDS = {"/reset", "/clear", "/start", "reset"}


class Bot:
    """Turns an inbound message into a reply and sends it."""

    def __init__(
        self,
        settings: Settings,
        store: ConversationStore,
        llm: LLMProvider,
        whatsapp: WhatsAppClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.llm = llm
        self.whatsapp = whatsapp

    def is_allowed(self, sender: str) -> bool:
        allowed = self.settings.allowed_numbers
        return not allowed or sender.lstrip("+") in allowed

    async def handle(self, message: IncomingMessage) -> str | None:
        """Process one message. Returns the reply text, or None if suppressed."""
        if not self.store.mark_processed(message.message_id):
            logger.info("Duplicate webhook for message %s, skipping", message.message_id)
            return None

        if not self.is_allowed(message.sender):
            logger.info("Ignoring message from unauthorized number %s", message.sender)
            return None

        await self.whatsapp.mark_read(message.message_id, message.phone_number_id)

        if message.text.lower() in RESET_COMMANDS:
            self.store.reset(message.sender)
            reply = "Started a fresh conversation. What would you like to talk about?"
            await self._send(message, reply)
            return reply

        self.store.add_message(message.sender, "user", message.text)

        try:
            reply = await self.llm.reply(
                system=self.settings.system_prompt,
                history=self.store.history(message.sender, self.settings.history_turns * 2),
            )
        except LLMError as exc:
            logger.error("LLM failure for %s: %s", message.sender, exc)
            reply = "Sorry, I'm having trouble thinking right now. Please try again in a moment."
        except Exception:
            logger.exception("Unexpected LLM error for %s", message.sender)
            reply = "Sorry, something went wrong on my side. Please try again."

        reply = self._trim(reply)
        if not reply:
            reply = "I didn't have a response for that - could you rephrase?"

        self.store.add_message(message.sender, "assistant", reply)
        await self._send(message, reply)
        return reply

    def _trim(self, text: str) -> str:
        text = (text or "").strip()
        limit = self.settings.max_reply_chars
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "\u2026"

    async def _send(self, message: IncomingMessage, reply: str) -> None:
        try:
            await self.whatsapp.send_text(
                to=message.sender, body=reply, phone_number_id=message.phone_number_id
            )
            logger.info("Replied to %s (%d chars)", message.sender, len(reply))
        except Exception:
            logger.exception("Failed to send reply to %s", message.sender)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = ConversationStore(settings.database_path)
    llm = build_provider(
        settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )
    whatsapp = WhatsAppClient(
        token=settings.whatsapp_token,
        phone_number_id=settings.phone_number_id,
        api_version=settings.graph_api_version,
        base_url=settings.graph_base_url,
    )
    bot = Bot(settings, store, llm, whatsapp)

    app = FastAPI(title="WhatsApp Realtime Chatbot", version="1.0.0")
    app.state.settings = settings
    app.state.store = store
    app.state.bot = bot

    for problem in settings.validate_for_live():
        logger.warning("Configuration: %s", problem)
    logger.info("LLM provider: %s", llm.name)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "llm_provider": llm.name,
            "whatsapp_configured": whatsapp.enabled,
            "signature_check": not settings.skip_signature_check,
        }

    @app.get("/webhook")
    async def verify_webhook(request: Request) -> Response:
        """Meta's verification handshake: echo hub.challenge when the token matches."""
        params = request.query_params
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == settings.verify_token
            and settings.verify_token
        ):
            logger.info("Webhook verified")
            return PlainTextResponse(params.get("hub.challenge", ""))
        logger.warning("Webhook verification failed")
        return PlainTextResponse("Forbidden", status_code=403)

    @app.post("/webhook")
    async def receive_webhook(request: Request, background: BackgroundTasks) -> Response:
        raw = await request.body()

        if settings.skip_signature_check:
            logger.warning("SKIP_SIGNATURE_CHECK is on - accepting unverified webhook")
        elif not verify_signature(
            settings.app_secret, raw, request.headers.get("X-Hub-Signature-256")
        ):
            logger.warning("Rejected webhook with invalid signature")
            return JSONResponse({"error": "invalid signature"}, status_code=403)

        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        batch = parse_webhook(payload)
        for status in batch.statuses:
            logger.debug("Delivery status: %s", status)

        for message in batch.messages:
            background.add_task(bot.handle, message)

        # Always 200 quickly, otherwise Meta retries and we double-reply.
        return JSONResponse({"status": "received", "queued": len(batch.messages)})

    @app.get("/")
    async def root() -> dict:
        return {
            "service": "whatsapp-realtime-chatbot",
            "webhook": "/webhook",
            "health": "/health",
        }

    return app


app = create_app()