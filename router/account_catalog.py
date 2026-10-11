"""Bounded account catalog cache. Never retain bearer tokens or HTTP request objects."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict

import httpx

from providers.upstream import chatgpt_headers, response_headers


def auto_context_limits(auto: dict, native_models: list[dict], route_models) -> dict:
    """Keep Auto within every configured model's advertised context limits."""
    native = {model.get("slug"): model for model in native_models}
    baseline = auto["context_window"]
    windows, maxima, percentages = [], [], []
    for slug in route_models:
        model = native.get(slug, {})
        window = model.get("context_window")
        if type(window) is not int or window <= 0:
            window = baseline
        maximum = model.get("max_context_window")
        if type(maximum) is not int or maximum < window:
            maximum = window
        percent = model.get("effective_context_window_percent")
        if type(percent) is not int or not 1 <= percent <= 100:
            percent = auto["effective_context_window_percent"]
        windows.append(window)
        maxima.append(maximum)
        percentages.append(percent)
    if not windows:
        return {}
    return {
        "context_window": min(windows),
        "max_context_window": min(maxima),
        "effective_context_window_percent": min(percentages),
    }


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
