"""Tests for the WhatsApp chatbot: webhook verification, reply flow, dedupe."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import EchoProvider
from app.main import Bot, create_app
from app.store import ConversationStore
from app.whatsapp import IncomingMessage, parse_webhook, verify_signature

SECRET = "test_app_secret"
VERIFY = "test_verify_token"


def make_settings(**overrides) -> Settings:
    base = dict(
        whatsapp_token="token",
        phone_number_id="123456",
        verify_token=VERIFY,
        app_secret=SECRET,
        llm_provider="echo",
        database_path=":memory:",
        skip_signature_check=False,
    )
    base.update(overrides)
    return Settings(**base)


def payload(text: str = "hello", message_id: str = "wamid.1", sender: str = "15551234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "123456",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": sender}
                            ],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeWhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.read: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send_text(self, to: str, body: str, phone_number_id: str | None = None) -> dict:
        self.sent.append((to, body))
        return {"messages": [{"id": "out.1"}]}

    async def mark_read(self, message_id: str, phone_number_id: str | None = None) -> None:
        self.read.append(message_id)


# ---> unit tests


def test_verify_signature_accepts_valid_and_rejects_tampered():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "wrong_secret"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=deadbeef")
    assert not verify_signature(SECRET, body + b" ", sign(body))


def test_parse_webhook_extracts_text_message():
    batch = parse_webhook(payload("hi there", "wamid.42", "15551112222"))
    assert len(batch.messages) == 1
    msg = batch.messages[0]
    assert msg.text == "hi there"
    assert msg.sender == "15551112222"
    assert msg.message_id == "wamid.42"
    assert msg.profile_name == "Test User"
    assert msg.phone_number_id == "123456"


def test_parse_webhook_ignores_non_text_and_foreign_objects():
    non_text = payload()
    non_text["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": "1555",
        "id": "wamid.img",
        "type": "image",
        "image": {"id": "abc"},
    }
    assert parse_webhook(non_text).messages == []
    assert parse_webhook({"object": "page", "entry": []}).messages == []


def test_parse_webhook_collects_statuses():
    p = payload()
    p["entry"][0]["changes"][0]["value"]["statuses"] = [
        {"id": "out.1", "status": "delivered"}
    ]
    batch = parse_webhook(p)
    assert len(batch.statuses) == 1
    assert batch.messages


# ---> store


def test_store_history_and_reset(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    for i in range(5):
        store.add_message("u1", "user", f"q{i}")
        store.add_message("u1", "assistant", f"a{i}")
    store.add_message("u2", "user", "other")

    history = store.history("u1", 4)
    assert [m["content"] for m in history] == ["q3", "a3", "q4", "a4"]
    assert all(m["role"] in {"user", "assistant"} for m in history)

    store.reset("u1")
    assert store.history("u1", 10) == []
    assert len(store.history("u2", 10)) == 1
    store.close()


def test_store_mark_processed_is_idempotent(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    assert store.mark_processed("wamid.1") is True
    assert store.mark_processed("wamid.1") is False
    assert store.mark_processed("wamid.2") is True
    store.close()


# ---> bot


@pytest.mark.asyncio
async def test_bot_replies_and_records_history(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(), store, EchoProvider(), wa)

    reply = await bot.handle(
        IncomingMessage(message_id="m1", sender="1555", text="ping")
    )
    assert reply == "(echo) You said: ping"
    assert wa.sent == [("1555", "(echo) You said: ping")]
    assert wa.read == ["m1"]
    assert [m["role"] for m in store.history("1555", 10)] == ["user", "assistant"]
    store.close()


@pytest.mark.asyncio
async def test_bot_ignores_duplicate_message_id(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(), store, EchoProvider(), wa)

    msg = IncomingMessage(message_id="dup", sender="1555", text="hi")
    assert await bot.handle(msg) is not None
    assert await bot.handle(msg) is None
    assert len(wa.sent) == 1
    store.close()


@pytest.mark.asyncio
async def test_bot_enforces_allowlist(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(allowed_numbers=("15550000000",)), store, EchoProvider(), wa)

    assert await bot.handle(IncomingMessage("m1", "15559999999", "hi")) is None
    assert wa.sent == []
    assert await bot.handle(IncomingMessage("m2", "15550000000", "hi")) is not None
    assert len(wa.sent) == 1
    store.close()


@pytest.mark.asyncio
async def test_bot_reset_command_clears_history(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(), store, EchoProvider(), wa)

    await bot.handle(IncomingMessage("m1", "1555", "hello"))
    assert len(store.history("1555", 10)) == 2

    reply = await bot.handle(IncomingMessage("m2", "1555", "/reset"))
    assert "fresh" in reply.lower()
    assert store.history("1555", 10) == []
    store.close()


@pytest.mark.asyncio
async def test_bot_truncates_overlong_reply(tmp_path):
    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(max_reply_chars=20), store, EchoProvider(), wa)

    reply = await bot.handle(IncomingMessage("m1", "1555", "x" * 200))
    assert len(reply) == 20
    assert reply.endswith("\u2026")
    store.close()


@pytest.mark.asyncio
async def test_llm_failure_sends_fallback(tmp_path):
    class Boom(EchoProvider):
        async def reply(self, system, history):
            raise RuntimeError("provider exploded")

    store = ConversationStore(tmp_path / "t.db")
    wa = FakeWhatsApp()
    bot = Bot(make_settings(), store, Boom(), wa)
    reply = await bot.handle(IncomingMessage("m1", "1555", "hi"))
    assert "went wrong" in reply.lower()
    assert len(wa.sent) == 1
    store.close()


# ---> http/https endpoints 


def test_webhook_verification_handshake(tmp_path):
    app = create_app(make_settings(database_path=str(tmp_path / "t.db")))
    with TestClient(app) as client:
        ok = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY,
                "hub.challenge": "12345",
            },
        )
        assert ok.status_code == 200
        assert ok.text == "12345"

        bad_token = client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
        )
        assert bad_token.status_code == 403

        wrong_mode = client.get(
            "/webhook",
            params={"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
        )
        assert wrong_mode.status_code == 403


def test_receiving_webhook_with_bad_signature_is_rejected(tmp_path):
    app = create_app(make_settings(database_path=str(tmp_path / "t.db")))
    with TestClient(app) as client:
        body = json.dumps(payload()).encode()
        res = client.post(
            "/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}
        )
        assert res.status_code == 403


def test_receiving_valid_webhook_queues_and_replies(tmp_path):
    app = create_app(make_settings(database_path=str(tmp_path / "t.db")))
    fake = FakeWhatsApp()
    app.state.bot.whatsapp = fake

    with TestClient(app) as client:
        body = json.dumps(payload("hello bot")).encode()
        res = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body),
                "Content-Type": "application/json",
            },
        )
        assert res.status_code == 200
        assert res.json()["queued"] == 1

    # TestClient runs background tasks before the response context exits.
    assert fake.sent == [("15551234567", "(echo) You said: hello bot")]

    with TestClient(app) as client:
        body = json.dumps(payload("hello bot")).encode()
        res = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body),
                "Content-Type": "application/json",
            },
        )
        assert res.status_code == 200
    assert len(fake.sent) == 1, "redelivered webhook must not reply twice"


def test_health_endpoint(tmp_path):
    app = create_app(make_settings(database_path=str(tmp_path / "t.db")))
    with TestClient(app) as client:
        data = client.get("/health").json()
        assert data["status"] == "ok"
        assert data["llm_provider"] == "echo"
        assert data["signature_check"] is True


def test_skip_signature_check_allows_unsigned_webhook(tmp_path):
    app = create_app(
        make_settings(
            database_path=str(tmp_path / "t.db"), skip_signature_check=True
        )
    )
    fake = FakeWhatsApp()
    app.state.bot.whatsapp = fake
    with TestClient(app) as client:
        res = client.post("/webhook", json=payload("no signature"))
        assert res.status_code == 200
    assert len(fake.sent) == 1