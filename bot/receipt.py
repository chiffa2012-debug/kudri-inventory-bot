"""Строгое распознавание и проверка магазинного чека по фото."""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, replace
from typing import Any

import httpx
from . import vision_provider


def _number(value: Any) -> float | None:
    if value is None or value == "" or value == "?":
        return None
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _money(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


@dataclass(frozen=True)
class ReceiptLine:
    title: str
    quantity: float | None
    unit_price: float | None
    line_total: float | None
    confidence: str = "low"
    issues: tuple[str, ...] = ()
    item_kind: str = ""  # empty preserves already saved legacy drafts
    requires_review: bool = False
    review_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "line_total": self.line_total,
            "confidence": self.confidence,
            "issues": list(self.issues),
            "item_kind": self.item_kind,
            "requires_review": self.requires_review,
            "review_reason": self.review_reason,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ReceiptLine":
        return cls(
            title=str(value.get("title") or "").strip(),
            quantity=_number(value.get("quantity")),
            unit_price=_number(value.get("unit_price")),
            line_total=_number(value.get("line_total")),
            confidence=str(value.get("confidence") or "low"),
            issues=tuple(str(item) for item in value.get("issues", [])),
            item_kind=str(value.get("item_kind") or ""),
            requires_review=value.get("requires_review") is True,
            review_reason=str(value.get("review_reason") or ""),
        )


@dataclass(frozen=True)
class ReceiptReadResult:
    lines: tuple[ReceiptLine, ...] = ()
    total: float | None = None
    purchased_at: str = ""
    merchant: str = ""
    raw: str = ""
    issues: tuple[str, ...] = ()
    calculated_total: float | None = None

    def to_dict(self) -> dict:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "total": self.total,
            "purchased_at": self.purchased_at,
            "merchant": self.merchant,
            "issues": list(self.issues),
            "calculated_total": self.calculated_total,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ReceiptReadResult":
        return cls(
            lines=tuple(ReceiptLine.from_dict(item) for item in value.get("lines", [])),
            total=_number(value.get("total")),
            purchased_at=str(value.get("purchased_at") or "").strip(),
            merchant=str(value.get("merchant") or "").strip(),
            issues=tuple(str(item) for item in value.get("issues", [])),
            calculated_total=_number(value.get("calculated_total")),
        )


def classify_receipt_line(line: ReceiptLine) -> ReceiptLine:
    title = " ".join(line.title.lower().replace("ё", "е").split())
    explicit_service = title in {"ламинирование ресниц", "ламинрование ресниц", "маникюр", "педикюр", "стрижка"} or title.startswith("услуга ")
    kind = "service" if explicit_service else line.item_kind
    if kind not in {"", "unknown", "service", "retail", "consumable"}:
        kind = "unknown"
    requires = line.requires_review or kind in {"unknown", "service", "retail"} or line.confidence not in {"high", "medium"} or bool(line.issues)
    reasons = {"service": "Похоже на услугу. Обычно на склад не добавляют.",
               "retail": "Возможно, товар для продажи. Уточните назначение.",
               "unknown": "По фото нельзя определить: расходник, товар для продажи или услуга."}
    return replace(line, item_kind=kind, requires_review=requires,
                   review_reason=reasons.get(kind, "Проверьте назначение позиции." if requires else ""))


def scanned_receipt(value: dict) -> ReceiptReadResult:
    return validate_receipt(ReceiptReadResult.from_dict({**value, "lines": [
        {**row, "item_kind": "unknown", "requires_review": True}
        for row in value.get("lines", []) if isinstance(row, dict)
    ]}))


def validate_receipt(receipt: ReceiptReadResult) -> ReceiptReadResult:
    checked_lines: list[ReceiptLine] = []
    all_line_totals_known = bool(receipt.lines)

    for source in receipt.lines:
        issues: list[str] = []
        if not source.title.strip():
            issues.append("не распознано название")
        if source.quantity is None:
            issues.append("не распознано количество")
        elif source.quantity <= 0:
            issues.append("количество должно быть больше нуля")
        if source.unit_price is None:
            issues.append("не распознана цена за единицу")
        elif source.unit_price < 0:
            issues.append("цена не может быть отрицательной")
        if source.line_total is None:
            issues.append("не распознана сумма строки")
            all_line_totals_known = False
        elif source.line_total < 0:
            issues.append("сумма строки не может быть отрицательной")
        if source.confidence not in {"high", "medium"}:
            issues.append("низкая уверенность распознавания")

        if (
            source.quantity is not None
            and source.unit_price is not None
            and source.line_total is not None
        ):
            expected = source.quantity * source.unit_price
            tolerance = max(1.0, abs(source.line_total) * 0.02)
            if abs(expected - source.line_total) > tolerance:
                issues.append(
                    "арифметика строки не сходится: "
                    f"{_money(source.quantity)} × {_money(source.unit_price)} ≠ "
                    f"{_money(source.line_total)}"
                )

        checked_lines.append(classify_receipt_line(replace(source, issues=tuple(issues))))

    calculated = (
        sum(line.line_total or 0 for line in checked_lines)
        if checked_lines and all_line_totals_known
        else None
    )
    receipt_issues = list(receipt.issues)
    if not checked_lines:
        receipt_issues.append("товарные строки не распознаны")
    if receipt.total is None:
        receipt_issues.append("итог чека не распознан")
    elif calculated is not None:
        tolerance = max(1.0, abs(receipt.total) * 0.02)
        if abs(receipt.total - calculated) > tolerance:
            receipt_issues.append(
                f"Итог чека {_money(receipt.total)} ₽, "
                f"сумма строк {_money(calculated)} ₽"
            )

    return replace(
        receipt,
        lines=tuple(checked_lines),
        issues=tuple(dict.fromkeys(receipt_issues)),
        calculated_total=calculated,
    )


def parse_manual_line(text: str) -> ReceiptLine:
    parts = [part.strip() for part in str(text or "").split("|")]
    if len(parts) != 4 or not parts[0]:
        raise ValueError(
            "Нужны четыре поля: Название | количество | цена за единицу | сумма"
        )
    return ReceiptLine(
        title=parts[0],
        quantity=_number(parts[1]),
        unit_price=_number(parts[2]),
        line_total=_number(parts[3]),
        confidence="high",
        item_kind="unknown",
        requires_review=True,
    )


def build_receipt_payload(
    receipt: ReceiptReadResult,
    *,
    sku_matches: list[dict | None],
    decisions: list[dict] | None = None,
) -> dict:
    decisions = list(decisions or [])
    items = []
    excluded_items = []
    for index, line in enumerate(receipt.lines):
        line = classify_receipt_line(line)
        match = sku_matches[index] if index < len(sku_matches) else None
        decision = decisions[index] if index < len(decisions) else {}
        skipped = decision.get("skip") or decision.get("reviewDecision") == "exclude"
        item = {
            "goodId": match.get("goodId") if match else None,
            "title": line.title,
            "amount": line.quantity,
            "pricePerUnit": line.unit_price,
            "lineTotal": line.line_total,
            "issues": list(line.issues),
            "categoryId": decision.get("categoryId"),
            "barcode": str(decision.get("barcode") or ""),
            "sourceIndex": index,
            "itemKind": line.item_kind,
            "requiresReview": line.requires_review,
            "reviewDecision": "exclude" if skipped else decision.get("reviewDecision"),
        }
        (excluded_items if skipped else items).append(item)
    excluded_total = None if any(row["lineTotal"] is None for row in excluded_items) else sum(row["lineTotal"] for row in excluded_items)
    return {
        "merchant": receipt.merchant or None,
        "purchasedAt": receipt.purchased_at or None,
        "total": receipt.total,
        "calculatedTotal": receipt.calculated_total,
        "issues": list(receipt.issues),
        "items": items,
        "excludedItems": excluded_items,
        "excludedTotal": excluded_total,
    }


async def read_receipt_from_bytes(
    *,
    api_key: str,
    model: str,
    file_bytes: bytes,
    timeout_s: float = 35.0,
    local_url: str = "",
    local_key: str = "",
) -> ReceiptReadResult:
    if local_url or local_key:
        parsed = await vision_provider.read_local_result(url=local_url,key=local_key,kind="receipt",file_bytes=file_bytes)
        try:
            if not isinstance(parsed,dict) or set(parsed)!={"merchant","purchased_at","total","lines"}:
                raise ValueError("Invalid receipt")
            if not isinstance(parsed["lines"],list) or len(parsed["lines"])>80 or not all(isinstance(row,dict) for row in parsed["lines"]):
                raise ValueError("Invalid receipt lines")
            # Recognition provides text, not a reliable purchase purpose. Never
            # silently treat a shampoo as a consumable rather than retail.
            return scanned_receipt(parsed)
        except (KeyError,TypeError,ValueError,AttributeError):
            return ReceiptReadResult(issues=("распознавание временно недоступно",))

    if not api_key:
        return ReceiptReadResult(issues=("OPENAI_API_KEY не задан",))
    if not file_bytes:
        return ReceiptReadResult(issues=("фото чека не получено",))

    image = base64.b64encode(file_bytes).decode("ascii")
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Разбери фото магазинного чека. Верни только JSON: "
                    '{"merchant":"","purchased_at":"YYYY-MM-DD или пусто",'
                    '"total":число или null,"lines":[{"title":"",'
                    '"quantity":число или null,"unit_price":число или null,'
                    '"line_total":число или null,"confidence":"high|medium|low"}]}.'
                    " Не вычисляй и не придумывай отсутствующие значения. "
                    "Скидки без товарной строки не превращай в товар."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Извлеки все товарные строки и услуги, их количества, цены и суммы из чека. Назначение покупки определит человек."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image}"},
                    },
                ],
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.RequestError:
        return ReceiptReadResult(issues=("OpenAI Vision недоступен",))

    if response.status_code >= 400:
        return ReceiptReadResult(issues=("OpenAI Vision вернул ошибку",))

    try:
        data = response.json()
        raw = data["choices"][0]["message"]["content"] or "{}"
        parsed = json.loads(raw)
        lines = tuple(
            ReceiptLine(
                title=str(item.get("title") or "").strip(),
                quantity=_number(item.get("quantity")),
                unit_price=_number(item.get("unit_price")),
                line_total=_number(item.get("line_total")),
                confidence=str(item.get("confidence") or "low"),
                item_kind="unknown",
                requires_review=True,
            )
            for item in parsed.get("lines", [])
            if isinstance(item, dict)
        )
        receipt = ReceiptReadResult(
            lines=lines,
            total=_number(parsed.get("total")),
            purchased_at=str(parsed.get("purchased_at") or "").strip(),
            merchant=str(parsed.get("merchant") or "").strip(),
            raw=raw,
        )
        return validate_receipt(receipt)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ReceiptReadResult(issues=("ответ Vision не удалось разобрать",))
