"""Распознавание этикетки расходника через OpenAI Vision.

Текстовый путь оставляем основным: если ключа OpenAI нет или модель не отвечает,
бот вернёт понятный статус и даст ввести данные вручную.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx

from .parsing import parse_barcode_like
from . import vision_provider


@dataclass(frozen=True)
class LabelReadResult:
    title: str
    barcode: str
    raw: str
    score: str = "low"


async def read_label_from_bytes(
    *,
    api_key: str,
    model: str,
    file_bytes: bytes,
    timeout_s: float = 25.0,
    local_url: str = "",
    local_key: str = "",
) -> LabelReadResult:
    """Пытаемся вытащить название + штрихкод с фото.

    Возвращаем `title` и `barcode` как минимум в пустом виде, чтобы caller не
    разруливал разные схемы ошибок.
    """

    if local_url or local_key:
        parsed = await vision_provider.read_local_result(url=local_url,key=local_key,kind="label",file_bytes=file_bytes)
        if (not isinstance(parsed,dict) or set(parsed)!={"title","barcode","score"}
                or not all(isinstance(parsed.get(k),str) for k in ("title","barcode","score"))):
            return LabelReadResult("", "", "")
        if len(parsed["title"])>180 or len(parsed["barcode"])>14 or parsed["score"] not in {"high","low"}:
            return LabelReadResult("", "", "")
        return LabelReadResult(parsed["title"].strip(),parsed["barcode"].strip(),"",parsed["score"])

    if not api_key:
        return LabelReadResult("", "", "")

    if not file_bytes:
        return LabelReadResult("", "", "")

    payload_image = base64.b64encode(file_bytes).decode("ascii")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Из текста с фото этикетки расходника извлеки только: "
                    "title (название товара), barcode (цифровой штрихкод если есть). "
                    "Верни JSON вида: {\"title\":\"...\",\"barcode\":\"...\",\"score\":\"high|low\"}."
                    "Если не уверен, оставь поля пустыми, но верни валидный JSON."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Нужен штрихкод/название расходника для склада."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{payload_image}"},
                    },
                ],
            },
        ],
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                json=body,
                headers=headers,
            )
        except httpx.RequestError:
            return LabelReadResult("", "", "")

    if response.status_code >= 400:
        return LabelReadResult("", "", "")

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"] or "{}"
        parsed = json.loads(text)
    except Exception:
        return LabelReadResult("", "", "")

    title = str(parsed.get("title") or "").strip()
    barcode = str(parsed.get("barcode") or "").strip()
    if not barcode:
        barcode = parse_barcode_like(text) or parse_barcode_like(title) or ""

    score = str(parsed.get("score") or "low")
    return LabelReadResult(title=title, barcode=barcode, raw=text, score=score)
