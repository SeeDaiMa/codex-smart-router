import time
from collections import OrderedDict

from router.types import RouteDecision


class RouteCache:
    """Bounded process-local routing state. Run a single worker for the MVP."""

    def __init__(self, capacity: int, ttl_seconds: int):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.entries: OrderedDict[str, tuple[float, RouteDecision]] = OrderedDict()

    def get(self, key: str) -> RouteDecision | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        expiry, decision = entry
        if expiry <= time.monotonic():
            del self.entries[key]
            return None
        self.entries.move_to_end(key)
        self.entries[key] = (time.monotonic() + self.ttl_seconds, decision)
        return decision.model_copy(deep=True)

    def put(self, key: str, decision: RouteDecision) -> None:
        self.entries[key] = (time.monotonic() + self.ttl_seconds, decision.model_copy(deep=True))
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def remember_response(self, response: dict, decision: RouteDecision) -> None:
        response_id = response.get("id")
        if isinstance(response_id, str) and response_id:
            self.put("response:" + response_id, decision)
