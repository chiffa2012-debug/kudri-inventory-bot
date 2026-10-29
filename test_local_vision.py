import base64
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiohttp import web

from bot import vision_provider
from bot.photo_intake import read_auto_from_bytes
from bot.config import Settings
from bot.vision import read_label_from_bytes
from bot.receipt import read_receipt_from_bytes
from hermes import inventory_vision_launcher as launcher

JPEG = b'\xff\xd8\xff' + b'test-image'
LABEL = {"title": "Перчатки", "barcode": "4601234567890", "score": "high"}
RECEIPT = {"merchant": "Магазин", "purchased_at": "2026-09-19", "total": 200,
           "lines": [{"title": "Перчатки", "quantity": 2, "unit_price": 100, "line_total": 200, "confidence": "high"}]}
URL = "http://127.0.0.1:8642/v1/inventory/vision"


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_provider_distinguishes_receipt_from_label(self):
        auto = {"kind": "receipt", "label": None, "receipt": RECEIPT}
        with patch.object(vision_provider, "read_local_result", AsyncMock(return_value=auto)):
            result = await read_auto_from_bytes(file_bytes=JPEG, local_url=URL, local_key="internal")
        self.assertEqual(result.kind, "receipt")
        self.assertEqual(result.receipt["merchant"], "Магазин")

    async def test_only_loopback_exact_endpoint_and_no_proxy_or_redirects(self):
        for bad in ["https://evil.test/v1/inventory/vision", "http://localhost/v1/inventory/vision",
                    "http://127.0.0.1/v1/chat/completions", "http://127.0.0.1/v1/inventory/vision?key=x",
                    "http://name:secret@127.0.0.1/v1/inventory/vision"]:
            with self.subTest(url=bad), patch.object(vision_provider.httpx, "AsyncClient") as client:
                self.assertIsNone(await vision_provider.read_local_result(url=bad, key="test", kind="label", file_bytes=JPEG))
                client.assert_not_called()
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "result": LABEL})
        original = httpx.AsyncClient
        def make_client(**kwargs):
            self.assertIs(kwargs["trust_env"], False)
            self.assertIs(kwargs["follow_redirects"], False)
            return original(transport=httpx.MockTransport(respond), **kwargs)
        with patch.object(vision_provider.httpx, "AsyncClient", side_effect=make_client):
            result = await vision_provider.read_local_result(url=URL,key="internal-only",kind="label",file_bytes=JPEG)
        self.assertEqual(result, LABEL)
        self.assertEqual(json.loads(seen[0].content), {"kind":"label","image_base64":base64.b64encode(JPEG).decode()})
        self.assertEqual(seen[0].headers["authorization"], "Bearer internal-only")

    async def test_local_provider_runs_without_openai_key_for_both_flows(self):
        with patch.object(vision_provider, "read_local_result", AsyncMock(return_value=LABEL)):
            result = await read_label_from_bytes(api_key="",model="unused",file_bytes=JPEG,local_url=URL,local_key="internal")
            self.assertEqual(result.title, "Перчатки")
        with patch.object(vision_provider, "read_local_result", AsyncMock(return_value=RECEIPT)):
            result = await read_receipt_from_bytes(api_key="",model="unused",file_bytes=JPEG,local_url=URL,local_key="internal")
            self.assertEqual(result.calculated_total, 200)
        settings = Settings(bot_token="test",api_base_url="http://local",api_key="test",
                            vision_provider="hermes-local",inventory_vision_url=URL,inventory_vision_key="internal")
        self.assertTrue(settings.vision_available)
        self.assertEqual(settings.vision_call_options(), {"local_url":URL,"local_key":"internal"})

    async def test_local_errors_never_fall_back_to_openai(self):
        with patch.object(vision_provider, "read_local_result", AsyncMock(return_value=None)), \
             patch("bot.vision.httpx.AsyncClient") as external:
            result=await read_label_from_bytes(api_key="should-not-be-used",model="x",file_bytes=JPEG,local_url=URL,local_key="test")
            self.assertEqual(result.title, "")
            external.assert_not_called()

    async def test_invalid_json_timeout_redirect_and_large_body_fall_back(self):
        responses = [httpx.Response(302,headers={"location":"https://evil.test/"}),
                     httpx.Response(200,text='not-json'),httpx.Response(200,json={"ok":True,"result":[]}),
                     httpx.Response(200,text='x'*70000),httpx.Response(503),
                     httpx.ReadTimeout("timeout")]
        original=httpx.AsyncClient
        for response in responses:
            def respond(request):
                if isinstance(response,Exception): raise response
                return response
            with patch.object(vision_provider.httpx,"AsyncClient",side_effect=lambda **kw:original(transport=httpx.MockTransport(respond),**kw)):
                self.assertIsNone(await vision_provider.read_local_result(url=URL,key="test",kind="receipt",file_bytes=JPEG))


class Content:
    def __init__(self,body): self.body=body
    async def iter_chunked(self,size):
        yield self.body


def request(payload, token="vision-key", remote="127.0.0.1", method="POST"):
    return SimpleNamespace(method=method,remote=remote,path=launcher.ENDPOINT,
                           headers={"Authorization":"Bearer "+token},content=Content(json.dumps(payload).encode()),
                           content_length=None)


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    def test_auto_schema_accepts_exactly_one_payload(self):
        auto = launcher.validate_result("auto", {"kind": "label", "label": LABEL, "receipt": None})
        self.assertEqual(auto["kind"], "label")
        self.assertEqual(auto["label"]["barcode"], "4601234567890")
        with self.assertRaises(ValueError):
            launcher.validate_result("auto", {"kind": "label", "label": LABEL, "receipt": RECEIPT})

    async def test_auth_loopback_and_method_block_model_before_body(self):
        invoke=AsyncMock(return_value=LABEL)
        handler=launcher.make_handler(key_getter=lambda:"vision-key",invoke=invoke)
        for token,remote,method,status in [("agent-api-key","127.0.0.1","POST",401),
                                           ("vision-key","8.8.8.8","POST",403),
                                           ("vision-key","127.0.0.1","GET",405)]:
            response=await handler(request({},token,remote,method))
            self.assertEqual(response.status,status)
        invoke.assert_not_awaited()

    async def test_rejects_caller_prompts_tools_invalid_base64_and_oversize(self):
        invoke=AsyncMock(return_value=LABEL)
        handler=launcher.make_handler(key_getter=lambda:"vision-key",invoke=invoke)
        valid={"kind":"label","image_base64":base64.b64encode(JPEG).decode()}
        for bad in [{**valid,"tools":["terminal"]},{**valid,"prompt":"ignore all"},
                    {**valid,"kind":"chat"},{**valid,"image_base64":"not-base64"},
                    {**valid,"image_base64":base64.b64encode(b'not-image').decode()},
                    {**valid,"image_base64":"A"*(launcher.MAX_REQUEST_BYTES+1)}]:
            response=await handler(request(bad))
            self.assertIn(response.status,[400,413])
        invoke.assert_not_awaited()

    async def test_auxiliary_call_has_fixed_prompt_no_tools_and_strict_result(self):
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(RECEIPT),tool_calls=None))])))))
        resolver=lambda:("openai-codex",client,"gpt-5.5")
        result=await launcher.call_auxiliary("receipt",base64.b64encode(JPEG).decode(),"image/jpeg",resolver=resolver)
        self.assertEqual(result,RECEIPT)
        kwargs=client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["tools"],[])
        self.assertEqual(kwargs["model"],"gpt-5.5")
        self.assertEqual(len(kwargs["messages"]),2)
        self.assertNotIn("session",kwargs)
        self.assertFalse(kwargs["store"])
        client.chat.completions.create.assert_awaited_once()

    async def test_success_schema_and_invalid_output_are_not_raw_provider_text(self):
        valid={"kind":"label","image_base64":base64.b64encode(JPEG).decode()}
        handler=launcher.make_handler(key_getter=lambda:"vision-key",invoke=AsyncMock(return_value=LABEL))
        response=await handler(request(valid))
        self.assertEqual(json.loads(response.text),{"ok":True,"result":LABEL})
        for invalid in [{**LABEL,"tools":[]},{"title":[]},["not-object"]]:
            with self.assertRaises(ValueError): launcher.validate_result("label",invalid)
        for invalid in [float('nan'),float('inf'),-1,True]:
            with self.assertRaises(ValueError): launcher.validate_result("receipt",{**RECEIPT,"total":invalid})
        handler=launcher.make_handler(key_getter=lambda:"vision-key",invoke=AsyncMock(side_effect=RuntimeError("private-image-or-token")))
        response=await handler(request(valid))
        self.assertEqual(response.status,503)
        self.assertNotIn("private",response.text)

    async def test_middleware_passes_existing_routes_to_original_auth(self):
        calls=[]
        async def original(req,handler):
            calls.append(req.path)
            return web.Response(status=401)
        module=SimpleNamespace(body_limit_middleware=original,web=web)
        launcher.install_hook(module,key_getter=lambda:"vision-key",invoke=AsyncMock(return_value=LABEL))
        req=request({},token="vision-key")
        req.path="/v1/chat/completions"
        response=await module.body_limit_middleware(req,None)
        self.assertEqual(response.status,401)
        self.assertEqual(calls,["/v1/chat/completions"])

    async def test_timeout_keeps_capacity_reserved_until_native_call_finishes(self):
        gate=asyncio.Event()
        started=asyncio.Event()
        native_tasks=[]
        async def invoke(*args):
            native_tasks.append(asyncio.current_task())
            started.set()
            await gate.wait()
            return LABEL
        handler=launcher.make_handler(key_getter=lambda:"vision-key",invoke=invoke)
        body={"kind":"label","image_base64":base64.b64encode(JPEG).decode()}
        real_timeout=asyncio.timeout
        deadlines=[]
        def controlled_timeout(_seconds):
            deadline=real_timeout(None)
            deadlines.append(deadline)
            return deadline
        with patch.object(launcher.asyncio,"timeout",side_effect=controlled_timeout):
            waiting=asyncio.create_task(handler(request(body)))
            await started.wait()
            # Expire only the first HTTP wait, after the native work is known to
            # be running. Successful requests have no artificial 20ms deadline.
            deadlines[0].reschedule(asyncio.get_running_loop().time()-1)
            response=await waiting
            self.assertEqual(response.status,503)
            self.assertEqual((await handler(request(body))).status,429)
            released=asyncio.Event()
            # Added after the handler's release callback: FIFO callback ordering
            # lets us observe actual slot release, independent of machine speed.
            native_tasks[0].add_done_callback(lambda _task:released.set())
            gate.set()
            await released.wait()
            self.assertEqual((await handler(request(body))).status,200)


if __name__ == '__main__': unittest.main()
