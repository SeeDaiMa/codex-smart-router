"""Create private local credentials without overwriting an existing .env."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env"
    router_key = "sr_" + secrets.token_urlsafe(32)
    gateway_key = "sk-" + secrets.token_urlsafe(32)
    text = (
        "SMART_ROUTER_AUTH_MODE=chatgpt\n"
        f"SMART_ROUTER_API_KEY={router_key}\n"
        "SMART_ROUTER_UPSTREAM_BASE_URL=http://127.0.0.1:4001/v1\n"
        f"SMART_ROUTER_UPSTREAM_API_KEY={gateway_key}\n"
        "SMART_ROUTER_PROJECT_ROOTS=[]\n"
        "SMART_ROUTER_CLASSIFIER_ENABLED=false\n"
        "SMART_ROUTER_CLASSIFIER_MODEL=gpt-6-luna\n"
        "OPENAI_API_KEY=\n"
        f"LITELLM_MASTER_KEY={gateway_key}\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env already exists; it was preserved.")
        return
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    print(f"Created private configuration: {path}")
    print("Default mode uses your Codex ChatGPT sign-in. No OPENAI_API_KEY is required.")


if __name__ == "__main__":
    main()
