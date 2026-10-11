import json
import re
from collections.abc import Callable

import httpx


def upstream_headers(key: str, incoming: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    # Do not forward Codex's credentials, org ids or its internal beta feature headers.
    for name in ("idempotency-key", "x-client-request-id"):
        if incoming and name in incoming:
            headers[name] = incoming[name]
    return headers


def chatgpt_headers(incoming: dict[str, str]) -> dict[str, str]:
    """Forward Codex-managed account auth without opening its credential store."""
    allowed = {
        "authorization",
        "accept",
        "chatgpt-account-id",
        "originator",
        "user-agent",
        "session_id",
        "openai-beta",
        "version",
        "x-client-request-id",
        "idempotency-key",
    }
    headers = {
        name: value
        for name, value in incoming.items()
        if name in allowed or name.startswith("x-codex-")
    }
    headers["content-type"] = "application/json"
    return headers


def response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name in {"content-type", "retry-after", "x-request-id"}
        or name.startswith("x-ratelimit-")
        or name.startswith("x-codex-")
    }


class SSEObserver:
    """Observe response ids without changing any byte of a provider's SSE stream."""

    def __init__(self, callback: Callable[[dict], None], max_event_bytes: int = 1024 * 1024):
        self.callback = callback
        self.buffer = b""
        self.max_event_bytes = max_event_bytes
        self.discarding = False

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while match := re.search(rb"\r?\n\r?\n", self.buffer):
            event, self.buffer = self.buffer[: match.start()], self.buffer[match.end() :]
            if self.discarding:
                self.discarding = False
                continue
            lines = [
                line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:")
            ]
            try:
                value = json.loads(b"\n".join(lines))
                if isinstance(value, dict) and isinstance(value.get("response"), dict):
                    self.callback(value["response"])
            except (ValueError, UnicodeDecodeError):
                pass
        if len(self.buffer) > self.max_event_bytes:
            self.buffer = self.buffer[-3:]
            self.discarding = True
