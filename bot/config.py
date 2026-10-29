"""Настройки бота учёта расходников.

Всё берётся из .env рядом с кодом (локально) или из EnvironmentFile юнита
systemd (на сервере). Значений по умолчанию у секретов нет намеренно: бот
должен падать на старте с понятной ошибкой, а не молча работать без доступа.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from .vision_provider import valid_local_endpoint

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_base_url: str
    api_key: str
    openai_api_key: str = ""
    openai_vision_model: str = "gpt-4o-mini"
    request_timeout_s: float = 25.0
    vision_provider: str = "openai"
    inventory_vision_url: str = ""
    inventory_vision_key: str = ""
    action_log_path: str = ""

    @property
    def vision_available(self) -> bool:
        if self.vision_provider == "hermes-local":
            return bool(self.inventory_vision_key and valid_local_endpoint(self.inventory_vision_url))
        return self.vision_provider == "openai" and bool(self.openai_api_key)

    def vision_call_options(self) -> dict:
        if self.vision_provider == "hermes-local":
            return {"local_url": self.inventory_vision_url, "local_key": self.inventory_vision_key}
        return {}


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    base = os.getenv("KUDRI_API_BASE_URL", "http://127.0.0.1:3007").strip().rstrip("/")
    key = os.getenv("INVENTORY_BOT_API_KEY", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_vision_model = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    missing = [
        name
        for name, value in (("BOT_TOKEN", token), ("INVENTORY_BOT_API_KEY", key))
        if not value
    ]
    if missing:
        raise SystemExit(
            "Не заполнены обязательные настройки: "
            + ", ".join(missing)
            + ".\nОткройте файл .env рядом с ботом и впишите значения."
        )

    return Settings(
        bot_token=token,
        api_base_url=base,
        api_key=key,
        openai_api_key=openai_api_key,
        openai_vision_model=openai_vision_model,
        vision_provider=os.getenv("VISION_PROVIDER", "openai").strip(),
        inventory_vision_url=os.getenv("INVENTORY_VISION_URL", "").strip(),
        inventory_vision_key=os.getenv("INVENTORY_VISION_KEY", "").strip(),
        action_log_path=os.getenv("INVENTORY_ACTION_LOG_PATH", "").strip(),
    )
