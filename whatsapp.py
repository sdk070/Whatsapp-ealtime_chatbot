"""WhatsApp Cloud API integration: payload parsing, verification, and sending."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"


@dataclass
class IncomingMessage:
    """A single text message extracted from a webhook payload."""

    message_id: str
    sender: str
    text: str
    timestamp: str = ""
    profile_name: str = ""
    phone_number_id: str = ""


@dataclass
class IncomingBatch:
    messages: list[IncomingMessage] = field(default_factory=list)
    statuses: list[dict[str, Any]] = field(default_factory=list)


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Validate Meta's ``X-Hub-Signature-256`` header against the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> IncomingBatch:
    """Flatten a Meta webhook payload into text messages and delivery statuses.

    Meta nests messages under ``entry[].changes[].value`` and may batch several
    entries per request. Non-text messages (images, audio, stickers, reactions)
    are skipped rather than guessed at, so we never reply nonsense.
    """
    batch = IncomingBatch()
    if payload.get("object") != "whatsapp_business_account":
        return batch

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id", "")
            contacts = {
                c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                for c in (value.get("contacts") or [])
            }

            for status in value.get("statuses") or []:
                batch.statuses.append(status)

            for msg in value.get("messages") or []:
                if msg.get("type") != "text":
                    logger.info(
                        "Ignoring non-text message type=%s id=%s",
                        msg.get("type"),
                        msg.get("id"),
                    )
                    continue
                sender = msg.get("from", "")
                text = ((msg.get("text") or {}).get("body") or "").strip()
                if not sender or not text:
                    continue
                batch.messages.append(
                    IncomingMessage(
                        message_id=msg.get("id", ""),
                        sender=sender,
                        text=text,
                        timestamp=msg.get("timestamp", ""),
                        profile_name=contacts.get(sender, ""),
                        phone_number_id=phone_number_id,
                    )
                )
    return batch


class WhatsAppClient:
    """Thin async client for the WhatsApp Cloud API send endpoint."""

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: str = "v21.0",
        timeout: float = 15.0,
        base_url: str = GRAPH_BASE,
    ) -> None:
        self.token = token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.phone_number_id)

    def _url(self, phone_number_id: str | None = None) -> str:
        pid = phone_number_id or self.phone_number_id
        return f"{self.base_url}/{self.api_version}/{pid}/messages"

    async def send_text(self, to: str, body: str, phone_number_id: str | None = None) -> dict:
        """Send a text message. Raises ``RuntimeError`` on a non-2xx response."""
        if not self.enabled:
            raise RuntimeError(
                "WhatsApp client is not configured: set WHATSAPP_TOKEN and "
                "WHATSAPP_PHONE_NUMBER_ID"
            )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self._url(phone_number_id),
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"WhatsApp send failed ({response.status_code}): {response.text}"
            )
        return response.json()

    async def mark_read(self, message_id: str, phone_number_id: str | None = None) -> None:
        """Mark an inbound message as read (shows blue ticks). Best effort."""
        if not self.enabled or not message_id:
            return
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(
                    self._url(phone_number_id),
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("Could not mark message %s as read: %s", message_id, exc)