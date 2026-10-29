"""Чистые функции для объединения нескольких снимков одной этикетки."""

from __future__ import annotations

from dataclasses import dataclass

from . import vision_provider
from .vision import LabelReadResult


@dataclass(frozen=True)
class AutoPhotoResult:
    kind: str
    label: dict | None = None
    receipt: dict | None = None


async def read_auto_from_bytes(*, file_bytes: bytes, local_url: str, local_key: str) -> AutoPhotoResult:
    parsed = await vision_provider.read_local_result(
        url=local_url, key=local_key, kind="auto", file_bytes=file_bytes, timeout_s=45.0
    )
    if not isinstance(parsed, dict) or set(parsed) != {"kind", "label", "receipt"}:
        return AutoPhotoResult("")
    kind = parsed.get("kind")
    if kind == "label" and isinstance(parsed.get("label"), dict) and parsed.get("receipt") is None:
        return AutoPhotoResult("label", label=parsed["label"])
    if kind == "receipt" and isinstance(parsed.get("receipt"), dict) and parsed.get("label") is None:
        return AutoPhotoResult("receipt", receipt=parsed["receipt"])
    return AutoPhotoResult("")


def merge_label_reads(reads: list[LabelReadResult]) -> LabelReadResult:
    useful = [item for item in reads if item.title or item.barcode]
    if not useful:
        return LabelReadResult("", "", "")

    ranked = sorted(useful, key=lambda item: item.score == "high", reverse=True)
    title = next((item.title.strip() for item in ranked if item.title.strip()), "")
    barcode = next((item.barcode.strip() for item in ranked if item.barcode.strip()), "")
    score = "high" if any(item.score == "high" for item in useful) else "low"
    return LabelReadResult(title, barcode, "", score)
