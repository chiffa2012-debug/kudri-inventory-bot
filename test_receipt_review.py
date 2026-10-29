import unittest
from unittest.mock import AsyncMock, patch

from bot.receipt import ReceiptLine, ReceiptReadResult, validate_receipt, build_receipt_payload, read_receipt_from_bytes
from bot import handlers
from bot.api_client import PanelClient
from test_receipt_flow import FakeState, FakeCall, FakePanel, FakeMessage


class ReceiptReviewTests(unittest.TestCase):
    def test_service_is_flagged_even_in_legacy_line(self):
        result = validate_receipt(ReceiptReadResult(lines=(ReceiptLine("Ламинирование ресниц", 1, 2000, 2000, "high"),)))
        self.assertEqual(result.lines[0].item_kind, "service")
        self.assertTrue(result.lines[0].requires_review)

    def test_lamination_material_is_not_claimed_to_be_service(self):
        result = validate_receipt(ReceiptReadResult(lines=(ReceiptLine("Состав для ламинирования ресниц", 1, 500, 500, "high"),)))
        self.assertNotEqual(result.lines[0].item_kind, "service")

    def test_excluded_lines_preserve_receipt_total_and_decisions(self):
        receipt = validate_receipt(ReceiptReadResult(lines=(
            ReceiptLine("Салфетки", 1, 100, 100, "high"),
            ReceiptLine("Ламинирование ресниц", 1, 2000, 2000, "high"),
        ), total=2100))
        payload = build_receipt_payload(receipt, sku_matches=[], decisions=[{"categoryId": 10}, {"skip": True}])
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["excludedItems"][0]["reviewDecision"], "exclude")
        self.assertEqual(payload["excludedTotal"], 2000)
        self.assertEqual(payload["total"], 2100)
        self.assertEqual(payload["calculatedTotal"], 2100)

    def test_manual_override_and_classification_survive_state_roundtrip(self):
        receipt = validate_receipt(ReceiptReadResult(lines=(ReceiptLine("Ламинирование ресниц", 1, 2000, 2000, "high"),)))
        restored = ReceiptReadResult.from_dict(receipt.to_dict())
        payload = build_receipt_payload(restored, sku_matches=[], decisions=[{"reviewDecision": "include", "categoryId": 10}])
        self.assertTrue(payload["items"][0]["requiresReview"])
        self.assertEqual(payload["items"][0]["reviewDecision"], "include")


class ScannedReceiptReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_scanned_lines_are_uncertain_until_human_decides(self):
        parsed = {"merchant": "Shop", "purchased_at": "", "total": 50, "lines": [{"title": "Шампунь", "quantity": 1, "unit_price": 50, "line_total": 50, "confidence": "high"}]}
        with patch("bot.receipt.vision_provider.read_local_result", AsyncMock(return_value=parsed)):
            result = await read_receipt_from_bytes(api_key="", model="", file_bytes=b"test", local_url="http://local", local_key="test")
        self.assertEqual(result.lines[0].item_kind, "unknown")
        self.assertTrue(result.lines[0].requires_review)

    async def test_ui_blocks_service_until_explicit_include_then_prepares_preview(self):
        receipt = validate_receipt(ReceiptReadResult(lines=(ReceiptLine("Ламинирование ресниц", 1, 2000, 2000, "high"),), total=2000))
        state = FakeState({"receipt": receipt.to_dict(), "receipt_decisions": [{"categoryId": 10}], "receipt_sku_matches": [None]})
        panel = FakePanel()
        await handlers.on_receipt_confirm(FakeCall(), state, panel)
        self.assertEqual(panel.preview_calls, [])
        line_call = FakeCall("receipt:line:0")
        await handlers.on_receipt_pick_line(line_call, state)
        buttons = [b.callback_data for row in line_call.message.edits[-1]["reply_markup"].inline_keyboard for b in row]
        self.assertIn("receipt:item:include:0", buttons)
        self.assertIn("Похоже на услугу", line_call.message.edits[-1]["text"])
        await handlers.on_receipt_toggle_line(FakeCall("receipt:item:include:0"), state)
        await handlers.on_receipt_confirm(FakeCall(), state, panel)
        self.assertEqual(len(panel.preview_calls), 1)
        self.assertEqual(panel.preview_calls[0]["items"][0]["reviewDecision"], "include")
        self.assertEqual(panel.confirm_calls, [], "preparing a reviewed receipt must still not write")

    async def test_excluded_service_is_forwarded_separately_and_shown_in_final_preview(self):
        receipt = validate_receipt(ReceiptReadResult(lines=(
            ReceiptLine("Салфетки", 1, 100, 100, "high"),
            ReceiptLine("Ламинирование ресниц", 1, 2000, 2000, "high"),
        ), total=2100))
        state = FakeState({"receipt": receipt.to_dict(), "receipt_decisions": [{"categoryId": 10}, {}]})
        await handlers.on_receipt_toggle_line(FakeCall("receipt:item:skip:1"), state)
        panel = FakePanel()
        panel.receipt_preview = AsyncMock(return_value={"token": "t", "fingerprint": "fp", "lines": [], "excludedLines": [{"title": "Ламинирование ресниц"}], "excludedTotal": 2000})
        call = FakeCall()
        await handlers.on_receipt_confirm(call, state, panel)
        kwargs = panel.receipt_preview.call_args.kwargs
        self.assertEqual(len(kwargs["items"]), 1)
        self.assertEqual(kwargs["excluded_items"][0]["reviewDecision"], "exclude")
        self.assertEqual(kwargs["total"], 2100)
        self.assertIn("2000 ₽", call.message.edits[-1]["text"])

    async def test_edit_invalidates_previous_review_decision(self):
        receipt = ReceiptReadResult(lines=(ReceiptLine("Салфетки", 1, 100, 100, "high"),))
        state = FakeState({"receipt": receipt.to_dict(), "receipt_edit_index": 0, "receipt_decisions": [{"reviewDecision": "include", "categoryId": 10}]})
        await handlers.on_receipt_edit_line_text(FakeMessage(text="Ламинирование ресниц | 1 | 2000 | 2000"), state, FakePanel())
        self.assertEqual(state.data["receipt_decisions"][0], {})
        self.assertTrue(state.data["receipt"]["lines"][0]["requires_review"])

    async def test_api_client_preserves_excluded_lines(self):
        fake = type("Client", (), {"_call": AsyncMock(return_value={"preview": {}})})()
        await PanelClient.receipt_preview(fake, telegram_id=77, items=[], excluded_items=[{"reviewDecision": "exclude", "lineTotal": 2000}])
        self.assertEqual(fake._call.call_args.kwargs["json"]["excludedItems"][0]["lineTotal"], 2000)


if __name__ == "__main__":
    unittest.main()
