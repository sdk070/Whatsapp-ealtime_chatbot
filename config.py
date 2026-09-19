"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    whatsapp_token: str = ""
    phone_number_id: str = ""
    verify_token: str = ""
    app_secret: str = ""
    graph_api_version: str = "v21.0"
    graph_base_url: str = "https://graph.facebook.com"

    llm_provider: str = "echo"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = ""
    system_prompt: str = "You are a helpful WhatsApp assistant."

    history_turns: int = 10
    max_reply_chars: int = 3500
    allowed_numbers: tuple[str, ...] = field(default_factory=tuple)
    skip_signature_check: bool = False

    database_path: str = "chatbot.db"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        allowed = os.getenv("ALLOWED_NUMBERS", "")
        return cls(
            whatsapp_token=os.getenv("WHATSAPP_TOKEN", ""),
            phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
            verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
            app_secret=os.getenv("WHATSAPP_APP_SECRET", ""),
            graph_api_version=os.getenv("GRAPH_API_VERSION", "v21.0"),
            graph_base_url=os.getenv("GRAPH_BASE_URL", "https://graph.facebook.com").rstrip("/"),
            llm_provider=os.getenv("LLM_PROVIDER", "echo").strip().lower(),
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", ""),
            system_prompt=os.getenv(
                "SYSTEM_PROMPT",
                "You are a helpful, friendly WhatsApp assistant. "
                "Reply in the user's language. Keep replies short and conversational.",
            ),
            history_turns=_int("HISTORY_TURNS", 10),
            max_reply_chars=_int("MAX_REPLY_CHARS", 3500),
            allowed_numbers=tuple(
                n.strip().lstrip("+") for n in allowed.split(",") if n.strip()
            ),
            skip_signature_check=_bool("SKIP_SIGNATURE_CHECK", False),
            database_path=os.getenv("DATABASE_PATH", "chatbot.db"),
            port=_int("PORT", 8000),
        )

    def validate_for_live(self) -> list[str]:
        """Return a list of problems that would prevent talking to WhatsApp."""
        problems: list[str] = []
        if not self.whatsapp_token:
            problems.append("WHATSAPP_TOKEN is not set")
        if not self.phone_number_id:
            problems.append("WHATSAPP_PHONE_NUMBER_ID is not set")
        if not self.verify_token:
            problems.append("WHATSAPP_VERIFY_TOKEN is not set")
        if not self.app_secret and not self.skip_signature_check:
            problems.append(
                "WHATSAPP_APP_SECRET is not set (set SKIP_SIGNATURE_CHECK=1 to test locally)"
            )
        return problems

    @property
    def db_file(self) -> Path:
        return Path(self.database_path)