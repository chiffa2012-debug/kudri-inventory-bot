"""Offline FSM regressions for packaging, material actions and batch counts."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from bot import handlers
from bot.keyboards import material_keyboard, shopping_keyboard
from bot.api_client import ApiError
from test_scan_flow import FakeState, FakeMessage, FakeCall, FakePanel


class MaterialUXTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_paint_tubes_have_correct_amount_and_total_price(self):
        state = FakeState({'title':'Краска','unit':'мл','category_id':10})
        await handlers.on_quantity_typed(FakeMessage(text='100'), state)
        self.assertEqual(state.state, handlers.Receive.waiting_package_count)
        await handlers.on_package_count(FakeMessage(text='3'), state)
        message = FakeMessage(text='450')
        await handlers.on_price_typed(message, state, FakePanel())
        text = message.answers[-1]['text']
        self.assertIn('300 мл', text)
        self.assertIn('1350 ₽', text)
        panel = FakePanel()
        call = FakeCall(message,'receive:confirm')
        await handlers.on_receive_confirm(call,state,panel)
        await handlers.on_receive_confirm(call,state,panel)
        self.assertEqual(len(panel.received),1)
        self.assertEqual(panel.received[0]['packagePrice'],450)
        self.assertTrue(panel.received[0]['idempotencyKey'])
        self.assertNotIn('450 ₽ за мл', message.answers[-1]['text'])

    async def test_pieces_and_package_count_are_whole_numbers(self):
        state = FakeState({'unit':'шт'})
        await handlers.on_quantity_typed(FakeMessage(text='1.5'),state)
        self.assertNotIn('package_size',state.data)
        state.data['package_size'] = 100
        await handlers.on_package_count(FakeMessage(text='1.5'),state)
        self.assertNotIn('package_count',state.data)
        await handlers.on_quantity_typed(FakeMessage(text='100'),state)
        await handlers.on_package_count(FakeMessage(text='2'),state)
        self.assertEqual(state.data['quantity'],200)

    async def test_failed_receive_can_retry_with_same_key(self):
        state = FakeState({'title':'Краска','unit':'мл','quantity':300,'package_size':100,'package_count':3,'price':450,'receive_request_id':'stable'})
        await state.set_state(handlers.Receive.waiting_receive_confirm)
        panel = FakePanel()
        panel.receive = AsyncMock(side_effect=ApiError('temporary','temporary'))
        with self.assertRaises(ApiError):
            await handlers.on_receive_confirm(FakeCall(FakeMessage(),'receive:confirm'),state,panel)
        self.assertEqual(state.state,handlers.Receive.waiting_receive_confirm)
        self.assertEqual(state.data['receive_request_id'],'stable')

    async def test_material_card_does_not_prepare_deletion(self):
        material = {'goodId':1,'title':'Краска','stock':300,'unit':'мл','editableFields':['minStock']}
        panel = FakePanel()
        panel.material = AsyncMock(return_value=material)
        panel.preview_good_delete = AsyncMock()
        msg = FakeMessage()
        await handlers.on_settings_good_info(FakeCall(msg,'settings:good:1'),FakeState(),panel)
        panel.preview_good_delete.assert_not_called()
        self.assertIn('300 мл',msg.answers[-1]['text'])
        self.assertIn('material:archive:1',{b.callback_data for row in msg.answers[-1]['reply_markup'].inline_keyboard for b in row})

    async def test_partial_writeoff_requires_preview_and_confirm(self):
        material = {'goodId':1,'title':'Краска','stock':300,'unit':'мл'}
        state = FakeState({'material':material,'material_kind':'writeoff'})
        panel = FakePanel()
        panel.material_preview = AsyncMock(return_value={'token':'p','stockBefore':300,'stockAfter':270})
        panel.material_confirm = AsyncMock(return_value={'material':{**material,'stock':270}})
        msg = FakeMessage(text='30')
        await handlers.on_material_value(msg,state,panel)
        panel.material_confirm.assert_not_called()
        self.assertIn('300 → 270 мл',msg.answers[-1]['text'])
        call = FakeCall(msg,'material:confirm')
        await handlers.on_material_confirm(call,state,panel)
        await handlers.on_material_confirm(call,state,panel)
        panel.material_confirm.assert_awaited_once_with(77,1,'writeoff','p')

    async def test_actual_zero_stock_is_valid(self):
        state = FakeState({'material':{'goodId':1,'title':'Краска','stock':100,'unit':'мл'},'material_kind':'count'})
        panel = FakePanel()
        panel.material_preview = AsyncMock(return_value={'token':'p','stockBefore':100,'stockAfter':0})
        await handlers.on_material_value(FakeMessage(text='0'),state,panel)
        panel.material_preview.assert_awaited_once_with(77,1,'count',stock=0.0)

    async def test_batch_next_keeps_category_but_clears_previous_good(self):
        state = FakeState({'category_id':10,'category_title':'Волосы','good_id':1,'barcode':'4600','quantity':300})
        await handlers.on_receive_next(FakeCall(FakeMessage(),'receive:next'),state)
        self.assertEqual(state.data['category_id'],10)
        self.assertNotIn('good_id',state.data)
        self.assertNotIn('barcode',state.data)
        self.assertEqual(state.state,handlers.Receive.waiting_scan_photo)

    async def test_batch_count_records_all_materials_before_one_confirmation(self):
        goods=[{'goodId':1,'title':'Краска','stock':100,'unit':'мл'},{'goodId':2,'title':'Палочки','stock':3,'unit':'шт'}]
        state=FakeState({'count_goods':goods,'count_category_id':10,'count_values':[]})
        panel=FakePanel()
        panel.inventory_count_preview=AsyncMock(return_value={'token':'batch','items':[{'goodId':1,'title':'Краска','stockBefore':100,'stockAfter':50,'unit':'мл','delta':-50}], 'changedCount':1})
        panel.inventory_count_confirm=AsyncMock(return_value={'status':'done','items':[]})
        await handlers.on_stock_count_value(FakeMessage(text='50'),state,panel)
        panel.inventory_count_preview.assert_not_called()
        await handlers.on_stock_count_value(FakeMessage(text='3'),state,panel)
        panel.inventory_count_preview.assert_awaited_once_with(77,10,[{'goodId':1,'stock':50.0},{'goodId':2,'stock':3.0}])
        panel.inventory_count_confirm.assert_not_called()
        await handlers.on_stock_count_confirm(FakeCall(FakeMessage(),'stock-count-confirm'),state,panel)
        await handlers.on_stock_count_confirm(FakeCall(FakeMessage(),'stock-count-confirm'),state,panel)
        panel.inventory_count_confirm.assert_awaited_once()

    def test_shopping_pages_do_not_exceed_telegram_keyboard(self):
        items=[{'goodId':1,'title':'Краска','ordered':False}]
        callbacks={b.callback_data for row in shopping_keyboard(items,1,3).inline_keyboard for b in row}
        self.assertIn('material:shopping-page:0',callbacks)
        self.assertIn('material:shopping-page:2',callbacks)
        self.assertIn('material:order:1:1',callbacks)

class MaterialReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_is_resolved_with_active_tenant_and_current_good(self):
        panel = FakePanel()
        panel.material = AsyncMock(return_value={"title":"Краска","photoFileId":"actual"})
        msg = FakeMessage()
        msg.answer_photo = AsyncMock()
        await handlers.on_material_photo(FakeCall(msg,"material:photo:1"),FakeState({"photoFileId":"other-tenant"}),panel)
        panel.material.assert_awaited_once_with(77,1)
        msg.answer_photo.assert_awaited_once_with("actual",caption="Краска")

    async def test_manual_title_always_checks_duplicate_and_uses_catalog_title(self):
        panel = FakePanel(exact={"goodId":55,"title":"Настоящая краска","stock":100,"unit":"мл"})
        state = FakeState({"scan_editing":False})
        await handlers.on_title_typed(FakeMessage(text="Краска"),state,panel)
        self.assertEqual(state.state,handlers.Receive.waiting_scan_confirm)
        self.assertEqual(state.data["candidate_title"],"Настоящая краска")

    async def test_existing_material_offers_saved_packaging(self):
        panel=FakePanel()
        panel.material=AsyncMock(return_value={"goodId":1,"title":"Краска","stock":100,"unit":"мл","packageSize":100})
        msg=FakeMessage()
        state=FakeState()
        await handlers.on_material_receive(FakeCall(msg,"material:receive:1"),state,panel)
        callbacks={b.callback_data for row in msg.answers[-1]["reply_markup"].inline_keyboard for b in row}
        self.assertIn("qty:100",callbacks)
        self.assertIn("qty:manual",callbacks)
        self.assertEqual(state.data["unit"],"мл")

    async def test_category_change_previews_before_any_write(self):
        panel=FakePanel()
        panel.material_preview=AsyncMock(return_value={"token":"category"})
        state=FakeState({"material":{"goodId":1,"title":"Краска","editableFields":["categoryId"]},"material_categories":[{"id":10,"title":"Волосы"}]})
        await handlers.on_material_category(FakeCall(FakeMessage(),"material:category:10"),state,panel)
        panel.material_preview.assert_awaited_once_with(77,1,"edit",patch={"categoryId":10})
        self.assertEqual(state.state,handlers.MaterialFlow.waiting_confirm)

    async def test_uncertain_batch_does_not_offer_blind_retry(self):
        state=FakeState({"count_preview":{"token":"batch"}})
        state.state=handlers.StockCount.waiting_confirm
        panel=FakePanel()
        panel.inventory_count_confirm=AsyncMock(return_value={"status":"partial","items":[{"errorCode":"uncertain_result"}]})
        msg=FakeMessage()
        await handlers.on_stock_count_confirm(FakeCall(msg,"stock-count-confirm"),state,panel)
        self.assertIsNone(state.state)
        self.assertIsNone(msg.answers[-1]["reply_markup"])
        self.assertIn("требуют проверки",msg.answers[-1]["text"])

    def test_card_history_shows_latest_five_and_cost_scope(self):
        material={"goodId":1,"title":"Краска","stock":100,"unit":"мл","categoryTitle":"Волосы","history":[{"kind":"receipt","amount":i,"createdAt":f"2026-09-{29-i:02}","actorId":77} for i in range(7)],"analytics":{"receiptCost30d":1350,"consumptionCost30d":45,"forecastNote":"Недостаточно данных"}}
        text=handlers.material_text(material)
        self.assertIn("Волосы",text)
        self.assertIn("1350 ₽",text)
        self.assertIn("45 ₽",text)
        self.assertIn("Недостаточно данных",text)
        self.assertIn("2026-09-29",text)
        self.assertNotIn("2026-09-23",text)
        self.assertIn("77",text)


if __name__=='__main__':
    unittest.main()
