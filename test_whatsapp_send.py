"""End-to-end test of the real Graph API send path against a local mock server.

The other tests stub the sender; this one runs actual httpx requests so the
request shape, auth header, and URL construction are verified.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.whatsapp import WhatsAppClient

RECEIVED: list[dict] = []


class _MockGraph(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": json.loads(self.rfile.read(length) or b"{}"),
            }
        )
        if "fail" in self.path:
            payload = json.dumps({"error": {"message": "bad token"}}).encode()
            self.send_response(401)
        else:
            payload = json.dumps({"messages": [{"id": "wamid.out"}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def mock_graph():
    RECEIVED.clear()
    server = HTTPServer(("127.0.0.1", 0), _MockGraph)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_send_text_hits_graph_api_correctly(mock_graph):
    client = WhatsAppClient(
        token="tok_123", phone_number_id="999888", api_version="v21.0", base_url=mock_graph
    )
    result = await client.send_text(to="15551234567", body="hello from the bot")

    assert result == {"messages": [{"id": "wamid.out"}]}
    assert len(RECEIVED) == 1
    req = RECEIVED[0]
    assert req["path"] == "/v21.0/999888/messages"
    assert req["auth"] == "Bearer tok_123"
    assert req["body"]["messaging_product"] == "whatsapp"
    assert req["body"]["to"] == "15551234567"
    assert req["body"]["type"] == "text"
    assert req["body"]["text"]["body"] == "hello from the bot"


async def test_send_text_uses_per_message_phone_number_id(mock_graph):
    client = WhatsAppClient(
        token="t", phone_number_id="default", api_version="v20.0", base_url=mock_graph
    )
    await client.send_text(to="1555", body="x", phone_number_id="override")
    assert RECEIVED[0]["path"] == "/v20.0/override/messages"


async def test_send_text_raises_on_error_status(mock_graph):
    client = WhatsAppClient(
        token="t", phone_number_id="1", api_version="v21.0", base_url=mock_graph
    )
    client._url = lambda pid=None: f"{mock_graph}/fail/path"  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="WhatsApp send failed"):
        await client.send_text(to="1555", body="x")


async def test_mark_read_posts_status_update(mock_graph):
    client = WhatsAppClient(
        token="t", phone_number_id="42", api_version="v21.0", base_url=mock_graph
    )
    await client.mark_read("wamid.in")
    assert RECEIVED[0]["body"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.in",
    }


async def test_disabled_client_raises():
    client = WhatsAppClient(token="", phone_number_id="")
    assert not client.enabled
    with pytest.raises(RuntimeError, match="not configured"):
        await client.send_text(to="1555", body="x")