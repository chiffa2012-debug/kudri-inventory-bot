"""Dedicated loopback OCR transport. Never holds provider OAuth credentials."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
from urllib.parse import urlsplit

import httpx

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 65536


def valid_local_endpoint(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
                and parsed.path == "/v1/inventory/vision" and not parsed.query and not parsed.fragment
                and parsed.username is None and parsed.password is None
                and (parsed.port is None or 0 < parsed.port <= 65535))
    except (ValueError, TypeError):
        return False


async def read_local_result(*, url: str, key: str, kind: str, file_bytes: bytes,
                            timeout_s: float = 65.0) -> dict | None:
    if not key or not valid_local_endpoint(url) or kind not in {"label", "receipt", "auto"}:
        return None
    if not file_bytes or len(file_bytes) > MAX_IMAGE_BYTES:
        return None
    try:
        async with asyncio.timeout(timeout_s):
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=3.0),
                                         trust_env=False, follow_redirects=False) as client:
                async with client.stream("POST", url, headers={"Authorization":f"Bearer {key}"},
                                         json={"kind":kind,"image_base64":base64.b64encode(file_bytes).decode("ascii")}) as response:
                    if response.status_code != 200:
                        return None
                    parts, size = [], 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > MAX_RESPONSE_BYTES:
                            return None
                        parts.append(part)
            body = json.loads(b"".join(parts))
            if not isinstance(body,dict) or body.get("ok") is not True or not isinstance(body.get("result"),dict):
                return None
            return body["result"]
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
        return None
