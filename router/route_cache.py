import logging
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from router.types import RouteDecision

logger = logging.getLogger(__name__)


class RouteCache:
    """Bounded memory cache backed by durable, non-expiring route identities."""

    def __init__(self, capacity: int, ttl_seconds: int, state_file: Path | None = None):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.state_file = state_file
        self.entries: OrderedDict[str, tuple[float, RouteDecision]] = OrderedDict()
        if state_file is not None:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.touch(mode=0o600, exist_ok=True)
            state_file.chmod(0o600)
            with closing(sqlite3.connect(state_file)) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS routes "
                    "(key TEXT PRIMARY KEY, decision TEXT NOT NULL)"
                )

    def get(self, key: str) -> RouteDecision | None:
        entry = self.entries.get(key)
        if entry is not None and entry[0] > time.monotonic():
            decision = entry[1]
        else:
            self.entries.pop(key, None)
            if self.state_file is None:
                return None
            try:
                with closing(sqlite3.connect(self.state_file)) as connection:
                    row = connection.execute(
                        "SELECT decision FROM routes WHERE key = ?", (key,)
                    ).fetchone()
                if row is None:
                    return None
                decision = RouteDecision.model_validate_json(row[0])
            except (sqlite3.Error, ValueError):
                logger.warning("Unable to read saved route; continuation recovery will be used")
                return None
        self._remember(key, decision)
        return decision.model_copy(deep=True)

    def _remember(self, key: str, decision: RouteDecision) -> None:
        self.entries[key] = (time.monotonic() + self.ttl_seconds, decision.model_copy(deep=True))
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def put(self, key: str, decision: RouteDecision) -> None:
        self.put_many([key], decision)

    def put_many(self, keys: list[str], decision: RouteDecision) -> None:
        for key in keys:
            self._remember(key, decision)
        if self.state_file is None or not keys:
            return
        try:
            with closing(sqlite3.connect(self.state_file)) as connection, connection:
                connection.executemany(
                    "INSERT INTO routes(key, decision) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET decision = excluded.decision",
                    [(key, decision.model_dump_json()) for key in dict.fromkeys(keys)],
                )
        except sqlite3.Error:
            logger.warning("Unable to save route; memory cache remains available")
