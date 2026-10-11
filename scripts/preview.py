"""Preview a route on the local server without exposing the router token."""

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("--project-root")
    args = parser.parse_args()
    settings = Settings(_env_file=ROOT / ".env")
    if not settings.api_key:
        raise SystemExit("Run scripts/init_env.py to initialize local credentials")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(
            "http://127.0.0.1:4000/v1/route",
            headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
            json={"task": args.task, "project_root": args.project_root},
        )
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
