"""Чек по фото: арифметика, preview, редактирование и отсутствие write."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import handlers
from bot.config import Settings
from bot.receipt import (
    ReceiptLine,
    ReceiptReadResult,
    build_receipt_payload,
    parse_manual_line,
    read_receipt_from_bytes,
    validate_receipt,
)


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.data.clear()
        self.state = None


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def get_file(self, _file_id):
        return SimpleNamespace(file_path="receipt.jpg")

    async def download_file(self, _path):
        return io.BytesIO(b"jpeg")

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeMessage:
    def __init__(self, *, text=None, message_id=100, bot=None):
        self.text = text
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=77)
        self.chat = SimpleNamespace(id=900)
        self.photo = [SimpleNamespace(file_id="receipt-large")]
        self.bot = bot or FakeBot()
        self.answers = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        sent = SimpleNamespace(message_id=1000 + len(self.answers))
        self.answers.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )
        return sent


class FakeCallbackMessage:
    def __init__(self):
        self.message_id = 500
        self.edits = []
        self.answers = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.answers.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )
        return SimpleNamespace(message_id=700 + len(self.answers))


class FakeCall:
    def __init__(self, data="receipt:confirm"):
        self.data = data
        self.from_user = SimpleNamespace(id=77)
        self.message = FakeCallbackMessage()
        self.answered = False

    async def answer(self, text=None, show_alert=False):
        self.answered = True


class FakePanel:
    def __init__(self):
        self.lookups = []
        self.receive_calls = []
        self.preview_calls = []
        self.confirm_calls = []
        self.confirm_result = {
            "status": "applied",
            "duplicate": False,
            "lines": [
                {"index": 0, "title": "Перчатки нитриловые M", "amount": 2, "status": "done", "error": None},
                {"index": 1, "title": "Салфетки", "amount": 1, "status": "done", "error": None},
            ],
        }

    async def whoami(self, _telegram_id):
        return {"id": "owner", "name": "Владелец"}

    async def find_good(self, *, telegram_id=None, title="", barcode=""):
        self.lookups.append((title, barcode))
        if title == "Перчатки нитриловые M":
            return {
                "exact": {
                    "goodId": 42,
                    "title": title,
                    "stock": 5,
                    "unit": "шт",
                },
                "matches": [],
            }
        return {"exact": None, "matches": []}

    async def receive(self, **payload):
        self.receive_calls.append(payload)
        raise AssertionError("старый одиночный receive не должен вызываться из чека")

    async def receipt_preview(self, *, telegram_id, items, merchant="", purchased_at="", total=None):
        self.preview_calls.append({
            "telegram_id": telegram_id,
            "items": items,
            "merchant": merchant,
            "purchased_at": purchased_at,
            "total": total,
        })
        return {
            "token": "token-1",
            "fingerprint": "fp-1",
            "expires_at": 0,
            "lines": [
                {
                    "index": index,
                    "title": item.get("title"),
                    "amount": item.get("amount"),
                    "pricePerUnit": item.get("pricePerUnit"),
                    "lineTotal": item.get("lineTotal"),
                }
                for index, item in enumerate(items)
            ],
        }

    async def receipt_confirm(self, *, telegram_id, token, fingerprint=None, confirmed=True):
        self.confirm_calls.append({
            "telegram_id": telegram_id,
            "token": token,
            "fingerprint": fingerprint,
            "confirmed": confirmed,
        })
        return self.confirm_result

    async def categories(self, _telegram_id=None):
        return [{"id": 10, "title": "Бытовое"}]



SETTINGS = Settings(
    bot_token="test",
    api_base_url="http://127.0.0.1:3007",
    api_key="test",
    openai_api_key="test",
)


GOOD_RECEIPT = ReceiptReadResult(
    lines=(
        ReceiptLine("Перчатки нитриловые M", 2, 150, 300, "high"),
        ReceiptLine("Салфетки", 1, 80, 80, "high"),
    ),
    total=380,
    purchased_at="2026-09-19",
    merchant="Магазин",
    raw="{}",
)


class ReceiptValidationTests(unittest.TestCase):
    def test_correct_receipt_has_no_arithmetic_issues(self):
        checked = validate_receipt(GOOD_RECEIPT)

        self.assertEqual(checked.calculated_total, 380)
        self.assertEqual(checked.issues, ())
        self.assertTrue(all(not line.issues for line in checked.lines))

    def test_total_mismatch_is_reported_without_changing_values(self):
        receipt = ReceiptReadResult(
            lines=(ReceiptLine("Перчатки", 2, 150, 300, "high"),),
            total=350,
            raw="{}",
        )

        checked = validate_receipt(receipt)

        self.assertEqual(checked.total, 350)
        self.assertEqual(checked.calculated_total, 300)
        self.assertIn("Итог чека 350 ₽, сумма строк 300 ₽", checked.issues)

    def test_partial_line_keeps_unknown_fields_and_is_flagged(self):
        receipt = ReceiptReadResult(
            lines=(ReceiptLine("Неясная позиция", None, 120, None, "low"),),
            total=120,
            raw="{}",
        )

        checked = validate_receipt(receipt)

        self.assertIsNone(checked.lines[0].quantity)
        self.assertIsNone(checked.lines[0].line_total)
        self.assertIn("не распознано количество", checked.lines[0].issues)
        self.assertIn("не распознана сумма строки", checked.lines[0].issues)
        self.assertIn("низкая уверенность распознавания", checked.lines[0].issues)

    def test_manual_line_requires_all_four_columns_but_allows_unknown(self):
        line = parse_manual_line("Салфетки | 2 | ? | 180")

        self.assertEqual(line.title, "Салфетки")
        self.assertEqual(line.quantity, 2)
        self.assertIsNone(line.unit_price)
        self.assertEqual(line.line_total, 180)

    def test_payload_contains_values_and_sku_decisions(self):
        receipt = validate_receipt(GOOD_RECEIPT)
        payload = build_receipt_payload(
            receipt,
            sku_matches=[{"goodId": 42}, None],
        )

        self.assertEqual(payload["items"][0]["goodId"], 42)
        self.assertEqual(payload["items"][1]["goodId"], None)
        self.assertEqual(payload["total"], 380)

    def test_payload_skips_retail_line_and_assigns_category_to_new_sku(self):
        receipt = validate_receipt(GOOD_RECEIPT)
        payload = build_receipt_payload(
            receipt,
            sku_matches=[None, None],
            decisions=[
                {"categoryId": 10, "barcode": "4602984004614"},
                {"skip": True},
            ],
        )

        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["title"], "Перчатки нитриловые M")
        self.assertEqual(payload["items"][0]["categoryId"], 10)
        self.assertEqual(payload["items"][0]["barcode"], "4602984004614")


class ReceiptFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_builds_preview_and_checks_each_sku(self):
        bot = FakeBot()
        state = FakeState({"prompt_message_id": 50})
        message = FakeMessage(bot=bot, message_id=100)
        panel = FakePanel()

        with patch.object(
            handlers,
            "read_receipt_from_bytes",
            AsyncMock(return_value=GOOD_RECEIPT),
        ):
            await handlers.on_receipt_photo(message, state, panel, SETTINGS)

        self.assertEqual(state.state, handlers.Receipt.waiting_preview)
        self.assertEqual(
            panel.lookups,
            [("Перчатки нитриловые M", ""), ("Салфетки", "")],
        )
        self.assertIn("найдена в базе", message.answers[-1]["text"])
        self.assertIn("выберите категорию", message.answers[-1]["text"])
        self.assertIn((900, 50), bot.deleted)
        self.assertIn((900, 1000), bot.deleted, "промежуточный статус распознавания удаляется")
        self.assertNotIn((900, 100), bot.deleted)

    async def test_confirmation_requests_preview_and_writes_nothing(self):
        receipt = validate_receipt(GOOD_RECEIPT)
        state = FakeState(
            {
                "receipt": receipt.to_dict(),
                "receipt_sku_matches": [{"goodId": 42}, None],
                "receipt_decisions": [{}, {"categoryId": 10}],
            }
        )
        call = FakeCall()
        panel = FakePanel()

        await handlers.on_receipt_confirm(call, state, panel)

        self.assertEqual(panel.receive_calls, [])
        self.assertEqual(len(panel.preview_calls), 1)
        self.assertEqual(panel.preview_calls[0]["items"][0]["goodId"], 42)
        self.assertEqual(panel.preview_calls[0]["items"][1]["goodId"], None)
        self.assertEqual(state.state, handlers.Receipt.waiting_write_confirm)
        self.assertEqual(state.data["receipt_write_token"], "token-1")
        self.assertIn("Подтвердите добавление на склад", call.message.edits[-1]["text"])

    async def test_category_choice_allows_new_receipt_sku_in_single_confirmation(self):
        receipt = validate_receipt(GOOD_RECEIPT)
        state = FakeState({
            "receipt": receipt.to_dict(),
            "receipt_sku_matches": [{"goodId": 42}, None],
            "receipt_decisions": [{}, {}],
        })
        call = FakeCall(data="receipt:item:cat:1:10")
        panel = FakePanel()

        await handlers.on_receipt_category_picked(call, state)
        self.assertEqual(state.data["receipt_decisions"][1]["categoryId"], 10)

        confirm = FakeCall(data="receipt:confirm")
        await handlers.on_receipt_confirm(confirm, state, panel)

        self.assertEqual(len(panel.preview_calls), 1)
        self.assertEqual(panel.preview_calls[0]["items"][1]["categoryId"], 10)

    async def test_write_confirms_and_shows_per_line_result(self):
        state = FakeState({"receipt_write_token": "token-1", "receipt_write_fingerprint": "fp-1"})
        state.state = handlers.Receipt.waiting_write_confirm
        call = FakeCall(data="receipt:write")
        panel = FakePanel()

        await handlers.on_receipt_write(call, state, panel)

        self.assertEqual(len(panel.confirm_calls), 1)
        self.assertEqual(panel.confirm_calls[0]["token"], "token-1")
        self.assertEqual(panel.confirm_calls[0]["fingerprint"], "fp-1")
        self.assertTrue(panel.confirm_calls[0]["confirmed"])
        self.assertIsNone(state.state)
        text = call.message.edits[-1]["text"]
        self.assertIn("✅ Перчатки нитриловые M", text)
        self.assertIn("✅ Салфетки", text)
        self.assertIn("Все строки записаны", text)

    async def test_partial_write_offers_resume_and_keeps_state(self):
        state = FakeState({"receipt_write_token": "token-1", "receipt_write_fingerprint": "fp-1"})
        state.state = handlers.Receipt.waiting_write_confirm
        call = FakeCall(data="receipt:write")
        panel = FakePanel()
        panel.confirm_result = {
            "status": "partial",
            "duplicate": False,
            "lines": [
                {"index": 0, "title": "Перчатки нитриловые M", "amount": 2, "status": "done", "error": None},
                {"index": 1, "title": "Салфетки", "amount": 1, "status": "failed", "error": "YC 500"},
            ],
        }

        await handlers.on_receipt_write(call, state, panel)

        self.assertEqual(state.state, handlers.Receipt.waiting_write_confirm)
        text = call.message.edits[-1]["text"]
        self.assertIn("❌ Салфетки", text)
        self.assertIn("Часть строк не записана", text)
        markup = call.message.edits[-1]["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("receipt:resume", callbacks)

    async def test_resume_continues_and_finishes(self):
        state = FakeState({"receipt_write_token": "token-1", "receipt_write_fingerprint": "fp-1"})
        state.state = handlers.Receipt.waiting_write_confirm
        call = FakeCall(data="receipt:resume")
        panel = FakePanel()

        await handlers.on_receipt_resume(call, state, panel)

        self.assertEqual(len(panel.confirm_calls), 1)
        self.assertIsNone(state.state)
        self.assertIn("Все строки записаны", call.message.edits[-1]["text"])


    async def test_editing_line_revalidates_and_rechecks_sku(self):
        receipt = validate_receipt(GOOD_RECEIPT)
        state = FakeState(
            {
                "receipt": receipt.to_dict(),
                "receipt_edit_index": 1,
                "receipt_sku_matches": [{"goodId": 42}, None],
            }
        )
        state.state = handlers.Receipt.waiting_edit_line
        message = FakeMessage(text="Салфетки большие | 2 | 50 | 100")
        panel = FakePanel()

        await handlers.on_receipt_edit_line_text(message, state, panel)

        self.assertEqual(state.state, handlers.Receipt.waiting_preview)
        self.assertEqual(state.data["receipt"]["lines"][1]["line_total"], 100)
        self.assertEqual(panel.lookups, [("Салфетки большие", "")])
        self.assertIn("Итог чека 380 ₽, сумма строк 400 ₽", message.answers[-1]["text"])

    async def test_cancel_clears_receipt_state(self):
        state = FakeState({"receipt": GOOD_RECEIPT.to_dict()})
        state.state = handlers.Receipt.waiting_preview
        call = FakeCall(data="cancel")

        await handlers.on_cancel(call, state)

        self.assertEqual(state.data, {})
        self.assertIsNone(state.state)

    async def test_no_openai_key_returns_empty_result_without_network(self):
        result = await read_receipt_from_bytes(
            api_key="",
            model="gpt-4o-mini",
            file_bytes=b"jpeg",
        )

        self.assertEqual(result.lines, ())
        self.assertIn("OPENAI_API_KEY не задан", result.issues)


if __name__ == "__main__":
    unittest.main()
