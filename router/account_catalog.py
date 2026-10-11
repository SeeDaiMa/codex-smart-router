"""Bounded account catalog cache. Never retain bearer tokens or HTTP request objects."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict

import httpx

from providers.upstream import chatgpt_headers, response_headers


class AccountCatalog:
    def __init__(self, client: httpx.AsyncClient, ttl: float = 300, capacity: int = 8):
        self.client = client
        self.ttl = ttl
        self.capacity = capacity
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, headers: dict[str, str], params) -> tuple[bytes, int, dict[str, str]]:
        # Include the credential's digest, account scope and query so one account or
        # token cannot reuse another's catalog. Only the digest is retained in memory.
        identity = json.dumps(
            [headers.get("authorization"), headers.get("chatgpt-account-id"), str(params)]
        ).encode()
        key = hashlib.sha256(identity).hexdigest()
        async with self.lock:
            entry = self.entries.get(key)
            if entry and entry[0] > time.monotonic():
                self.entries.move_to_end(key)
                return entry[1]
            upstream = await self.client.get(
                "models", params=params, headers=chatgpt_headers(headers)
            )
            result = (upstream.content, upstream.status_code, response_headers(upstream))
            if upstream.is_success and len(upstream.content) <= 2 * 1024 * 1024:
                try:
                    catalog = upstream.json()
                    valid = isinstance(catalog, dict) and isinstance(catalog.get("models"), list)
                except ValueError:
                    valid = False
                if valid:
                    self.entries[key] = (time.monotonic() + self.ttl, result)
                    self.entries.move_to_end(key)
                    while len(self.entries) > self.capacity:
                        self.entries.popitem(last=False)
            return result
