"""Codex credential command: stdout is consumed by Codex, not shown to the model."""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    key = os.environ.get("SMART_ROUTER_API_KEY") or dotenv_values(ROOT / ".env").get(
        "SMART_ROUTER_API_KEY"
    )
    if not key:
        print("Router credentials are not configured; run scripts/init_env.py", file=sys.stderr)
        return 1
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
