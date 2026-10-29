"""Inventory campaign interactions; all network boundaries use local fakes."""
import unittest
import json
import httpx
from copy import deepcopy
from types import SimpleNamespace

from bot import cycle
from bot.api_client import PanelClient, ApiError
from bot.config import Settings
from bot.keyboards import main_menu, BTN_INVENTORY
from test_receipt_flow import FakeState, FakeMessage, FakeBot


SNAPSHOT = {
    "campaign": {"id": "campaign-1", "status": "active", "categories": [], "nextCycleDueAt": None},
    "progress": {"total": 2, "completed": 0},
    "currentCategory": {"id": "cat-1", "title": "Перчатки <M>", "status": "pending", "dueAt": "2026-09-19"},
    "outbox": [],
}


class Panel:
    def __init__(self):
        self.snapshot = deepcopy(SNAPSHOT)
        self.actions = []
        self.previews = []

    async def whoami(self, telegram_id):
        return {"id": "owner", "name": "Владелец"}

    async def inventory_cycle(self, telegram_id):
        return deepcopy(self.snapshot)

    async def cycle_action(self, telegram_id, campaign_id, category_id, action, **payload):
        self.actions.append((telegram_id, campaign_id, category_id, action, payload))
        if action == "complete":
            self.snapshot["currentCategory"] = None
            self.snapshot["progress"]["completed"] = 1
        else:
            self.snapshot["currentCategory"]["status"] = "needs_recheck" if action == "recheck" else "in_progress"
        if action == "recheck":
            self.snapshot["campaign"]["revision"] = self.snapshot["campaign"].get("revision", 0) + 1
        return deepcopy(self.snapshot)

    async def cycle_preview(self, **payload):
        self.previews.append(payload)
        return {"goodId": 42, "title": "Перчатки", "expected": 5, "counted": 3, "delta": -2, "writeEnabled": False}

    async def receive(self, **payload):
        raise AssertionError("No warehouse writes")

    async def deplete(self, *args):
        raise AssertionError("No warehouse writes")


class Callback:
    def __init__(self, data, message_id=1000):
        self.data = data
        self.from_user = SimpleNamespace(id=77)
        self.message = FakeMessage(message_id=message_id)
        self.message.edits = []
        async def edit_text(text, reply_markup=None, parse_mode=None):
            self.message.edits.append({"text": text, "reply_markup": reply_markup})
        self.message.edit_text = edit_text
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


class CycleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def begin(self):
        state, panel, message = FakeState({"prompt_message_id": 50}), Panel(), FakeMessage()
        await cycle.on_inventory(message, state, panel)
        return state, panel, message

    async def test_menu_and_entry_remove_previous_prompt_escape_title(self):
        state, panel, message = await self.begin()
        self.assertIn(BTN_INVENTORY, [b.text for row in main_menu().keyboard for b in row])
        self.assertEqual(message.bot.deleted, [(900, 50)])
        self.assertIn("0/2", message.answers[-1]["text"])
        self.assertIn("&lt;M&gt;", message.answers[-1]["text"])
        self.assertEqual(state.data["cycle_campaign_id"], "campaign-1")

    async def test_actions_bind_authenticated_user_and_campaign_without_stock_writes(self):
        state, panel, _ = await self.begin()
        for action in ["start", "continue", "recheck"]:
            call = Callback("cycle:" + action)
            await cycle.on_cycle_callback(call, state, panel)
            self.assertEqual(panel.actions[-1][:4], (77, "campaign-1", "cat-1", action))
            self.assertTrue(call.message.edits)

    async def test_complete_requires_valid_counts_preview_then_explicit_confirm(self):
        state, panel, _ = await self.begin()
        await cycle.on_cycle_callback(Callback("cycle:complete"), state, panel)
        self.assertEqual(panel.actions, [])
        for bad in ["-1 | 0", "2 | 3", "1.5 | 0", "abc", "0 | -1", "9" * 5000 + " | 0"]:
            await cycle.on_cycle_counts(FakeMessage(text=bad), state, panel)
            self.assertEqual(state.state, cycle.Inventory.waiting_counts)
        await cycle.on_cycle_counts(FakeMessage(text="12 | 2"), state, panel)
        self.assertEqual(panel.actions, [])
        self.assertEqual(state.state, cycle.Inventory.waiting_confirmation)
        await cycle.on_cycle_callback(Callback("cycle:confirm", state.data["prompt_message_id"]), state, panel)
        self.assertEqual(panel.actions[-1], (77, "campaign-1", "cat-1", "complete", {"checkedSkuCount": 12, "discrepancies": 2, "confirmed": True, "revision": 0}))
        self.assertNotIn("cycle_counts", state.data)

    async def test_unprepared_or_stale_confirmation_cannot_complete(self):
        state, panel, _ = await self.begin()
        await cycle.on_cycle_callback(Callback("cycle:confirm"), state, panel)
        await cycle.on_cycle_callback(Callback("cycle:start", 99), state, panel)
        self.assertEqual(panel.actions, [])

    async def test_back_clears_state_restores_menu_and_removes_bot_prompt(self):
        state, panel, _ = await self.begin()
        call = Callback("cycle:menu")
        await cycle.on_cycle_callback(call, state, panel)
        self.assertIsNone(state.state)
        self.assertNotIn("cycle_campaign_id", state.data)
        self.assertEqual(call.message.bot.deleted, [(900, 1000)])
        self.assertIsNotNone(call.message.answers[-1]["reply_markup"].keyboard)

    async def test_empty_catalog_has_no_category_actions(self):
        panel, state, message = Panel(), FakeState(), FakeMessage()
        panel.snapshot.update(currentCategory=None, progress={"total": 0, "completed": 0})
        panel.snapshot["campaign"]["status"] = "empty"
        await cycle.on_inventory(message, state, panel)
        callbacks = [b.callback_data for row in message.answers[-1]["reply_markup"].inline_keyboard for b in row]
        self.assertNotIn("cycle:complete", callbacks)

    async def test_sku_count_only_produces_preview(self):
        state, panel, _ = await self.begin()
        await cycle.on_cycle_callback(Callback("cycle:preview"), state, panel)
        await cycle.on_cycle_sku(FakeMessage(text="42 | 3"), state, panel)
        self.assertEqual(panel.actions, [])
        self.assertEqual(panel.previews, [{"telegram_id": 77, "campaign_id": "campaign-1", "category_id": "cat-1", "good_id": 42, "counted": 3.0, "revision": 0}])
        self.assertEqual(state.data["cycle_sku_preview"]["delta"], -2)

    async def test_panel_client_uses_protected_cycle_routes_and_preserves_counts(self):
        requests = []
        def transport(request):
            requests.append(request)
            if request.url.path.endswith("/preview"):
                return httpx.Response(200, json={"ok": True, "preview": {"delta": -2, "writeEnabled": False}})
            return httpx.Response(200, json={"ok": True, "cycle": SNAPSHOT})
        panel = PanelClient(Settings(bot_token="test", api_base_url="http://local.test", api_key="test-cycle-key"))
        await panel.close()
        panel._client = httpx.AsyncClient(transport=httpx.MockTransport(transport), headers={"x-inventory-key": "test-cycle-key"})
        try:
            await panel.inventory_cycle(77)
            await panel.cycle_action(77, "campaign-1", "cat-1", "complete", checkedSkuCount=12, discrepancies=2, confirmed=True, revision=2)
            preview = await panel.cycle_preview(telegram_id=77, campaign_id="campaign-1", category_id="cat-1", good_id=42, counted=3, revision=2)
        finally:
            await panel.close()
        self.assertEqual([(r.method, r.url.path) for r in requests], [
            ("GET", "/api/inventory-bot/cycle"),
            ("POST", "/api/inventory-bot/cycle/categories/cat-1/complete"),
            ("POST", "/api/inventory-bot/cycle/preview"),
        ])
        self.assertEqual(requests[0].url.params["telegramId"], "77")
        self.assertTrue(all(r.headers["x-inventory-key"] == "test-cycle-key" for r in requests))
        self.assertEqual(json.loads(requests[1].content), {"telegramId": 77, "campaignId": "campaign-1", "checkedSkuCount": 12, "discrepancies": 2, "confirmed": True, "revision": 2})
        self.assertEqual(json.loads(requests[2].content)["revision"], 2)
        self.assertFalse(preview["writeEnabled"])

    async def test_api_failure_leaves_retry_menu_without_false_completion(self):
        state, panel, _ = await self.begin()
        async def fail(*args, **kwargs):
            raise ApiError("Категория изменена. Обновите расписание.")
        panel.cycle_action = fail
        call = Callback("cycle:start")
        await cycle.on_cycle_callback(call, state, panel)
        self.assertIn("Обновите", call.message.edits[-1]["text"])
        self.assertEqual(panel.actions, [])

    async def test_recheck_updates_revision_for_next_completion(self):
        state, panel, _ = await self.begin()
        await cycle.on_cycle_callback(Callback("cycle:recheck"), state, panel)
        self.assertEqual(panel.actions[-1][-1]["revision"], 0)
        self.assertEqual(state.data["cycle_revision"], 1)
        await cycle.on_cycle_callback(Callback("cycle:complete"), state, panel)
        await cycle.on_cycle_counts(FakeMessage(text="5 | 1"), state, panel)
        await cycle.on_cycle_callback(Callback("cycle:confirm", state.data["prompt_message_id"]), state, panel)
        self.assertEqual(panel.actions[-1][-1]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
