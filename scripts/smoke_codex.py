"""Exercise the installed Codex CLI through the real router against a local fake provider.

No OpenAI request or billable model call is made. The fake provider asks Codex to
execute `pwd` once, then returns a fixed marker through the normal Responses stream.
"""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import SecretStr

# Support both `python scripts/smoke_codex.py` and an installed editable package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from server.app import create_app  # noqa: E402

MARKER = "SMART_ROUTER_LOCAL_SMOKE_OK"


def events(response_id: str, model: str, output: dict):
    base = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
    }
    items = [
        {"type": "response.created", "response": base},
        {"type": "response.output_item.added", "output_index": 0, "item": output},
    ]
    if output["type"] == "message":
        items.append(
            {
                "type": "response.output_text.delta",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": MARKER,
            }
        )
    items.extend(
        [
            {"type": "response.output_item.done", "output_index": 0, "item": output},
            {
                "type": "response.completed",
                "response": {
                    **base,
                    "status": "completed",
                    "output": [output],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            },
        ]
    )
    for index, item in enumerate(items):
        yield (
            "event: "
            + item["type"]
            + "\ndata: "
            + json.dumps({**item, "sequence_number": index}, ensure_ascii=False)
            + "\n\n"
        ).encode()


def launch(app):
    # Keep the bound socket open so another process cannot steal the selected port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            sock.close()
            raise RuntimeError("Local test server did not start")
        time.sleep(0.02)
    return server, thread, sock, port


def launch_gateway(upstream_port, upstream_key, directory):
    """Start the actual LiteLLM proxy with all inference pointing to the fake provider."""
    gateway_key = "sk-" + secrets.token_urlsafe(32)
    config = yaml.safe_load((ROOT / "config" / "litellm.yaml").read_text())
    for item in config["model_list"]:
        item["litellm_params"]["api_key"] = upstream_key
        item["litellm_params"]["api_base"] = f"http://127.0.0.1:{upstream_port}/v1"
    config["general_settings"]["master_key"] = gateway_key
    path = directory / "litellm.yaml"
    path.write_text(yaml.safe_dump(config))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(
        {
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_TELEMETRY": "False",
            "OPENAI_API_KEY": upstream_key,
            "LITELLM_MASTER_KEY": gateway_key,
            "NO_PROXY": "127.0.0.1,localhost",
            "LITELLM_LOG": "ERROR",
        }
    )
    log = (directory / "gateway.log").open("w+")
    process = subprocess.Popen(
        [
            str(ROOT / ".venv" / "bin" / "litellm"),
            "--config",
            str(path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    with httpx.Client(timeout=1, trust_env=False) as probe:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                response = probe.get(f"http://127.0.0.1:{port}/health/liveliness")
                if response.status_code == 200:
                    return process, log, port, gateway_key
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    log.seek(0)
    error = log.read()[-4000:]
    log.close()
    raise RuntimeError("LiteLLM did not start: " + error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="codex")
    parser.add_argument("--report", type=Path, default=ROOT / ".run" / "smoke-report.json")
    parser.add_argument("--with-litellm", action="store_true")
    parser.add_argument("--account-auth", action="store_true")
    args = parser.parse_args()
    if args.account_auth and args.with_litellm:
        parser.error("Account mode uses the native backend directly, not LiteLLM")
    calls = []
    received_encodings = []
    catalog_calls = []
    upstream_key = secrets.token_urlsafe(32)
    router_key = secrets.token_urlsafe(32)
    fake = FastAPI()
    auth_seen = {"bearer_present": False, "account_id_present": False}

    @fake.get("/v1/models")
    async def native_models():
        catalog_calls.append(True)
        model = json.loads((ROOT / "config" / "codex-models.json").read_text())["models"][0]
        return {"models": [{**model, "slug": "gpt-5.6-sol", "display_name": "Sol"}]}

    @fake.post("/v1/responses")
    async def respond(request: Request):
        if args.account_auth:
            auth_seen["bearer_present"] = request.headers.get("authorization", "").startswith(
                "Bearer "
            )
            auth_seen["account_id_present"] = bool(request.headers.get("chatgpt-account-id"))
            assert auth_seen["bearer_present"]
        else:
            assert request.headers["authorization"] == "Bearer " + upstream_key
        payload = await request.json()
        calls.append(payload)
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        if not outputs:
            names = [item.get("name") for item in payload.get("tools", [])]
            assert "exec_command" in names, f"Expected exec_command; tools={names}"
            output = {
                "type": "function_call",
                "id": "fc_smoke",
                "call_id": "call_smoke",
                "name": "exec_command",
                "arguments": json.dumps(
                    {
                        "cmd": "pwd",
                        "max_output_tokens": 64,
                    }
                ),
            }
        else:
            assert outputs[-1]["call_id"] == "call_smoke"
            output = {
                "type": "message",
                "id": "msg_smoke",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": MARKER, "annotations": []},
                ],
            }
        return StreamingResponse(
            events(f"resp_smoke_{len(calls)}", payload["model"], output),
            media_type="text/event-stream",
        )

    servers = []
    gateway = None
    temporary = None
    result = None
    try:
        upstream = launch(fake)
        servers.append(upstream)
        upstream_url = f"http://127.0.0.1:{upstream[3]}/v1"
        router_upstream_key = upstream_key
        args.report.parent.mkdir(parents=True, exist_ok=True)
        if args.with_litellm:
            temporary = tempfile.TemporaryDirectory(prefix="gateway-smoke-", dir=args.report.parent)
            gateway = launch_gateway(upstream[3], upstream_key, Path(temporary.name))
            upstream_url = f"http://127.0.0.1:{gateway[2]}/v1"
            router_upstream_key = gateway[3]
        settings = Settings(
            _env_file=None,
            auth_mode="chatgpt" if args.account_auth else "api",
            api_key=SecretStr(router_key),
            upstream_api_key=SecretStr(router_upstream_key),
            upstream_base_url=upstream_url,
            chatgpt_base_url=upstream_url,
            classifier_enabled=False,
            project_roots=[],
        )
        app = create_app(settings)

        @app.middleware("http")
        async def record_encoding(request: Request, call_next):
            if request.method == "POST":
                received_encodings.append(request.headers.get("content-encoding", "identity"))
            return await call_next(request)

        router = launch(app)
        servers.append(router)
        config = {
            "model": "smart-auto",
            "model_provider": "smart-router",
            "model_catalog_json": str(ROOT / "config" / "codex-models.json"),
            "model_reasoning_effort": "medium",
            "model_providers.smart-router.name": "Smart Router",
            "model_providers.smart-router.base_url": f"http://127.0.0.1:{router[3]}/v1",
            "model_providers.smart-router.auth.command": str(ROOT / ".venv" / "bin" / "python"),
            "model_providers.smart-router.auth.args": [
                str(ROOT / "scripts" / "print_router_token.py")
            ],
            "model_providers.smart-router.auth.timeout_ms": 5000,
            "model_providers.smart-router.wire_api": "responses",
            "model_providers.smart-router.supports_websockets": False,
            "model_providers.smart-router.requires_openai_auth": False,
            "model_providers.smart-router.request_max_retries": 0,
            "model_providers.smart-router.stream_max_retries": 0,
            "sqlite_home": str(args.report.parent / "codex-state"),
            "log_dir": str(args.report.parent / "codex-logs"),
        }
        if args.account_auth:
            config.pop("model_catalog_json")
            for key in (
                "model_providers.smart-router.auth.command",
                "model_providers.smart-router.auth.args",
                "model_providers.smart-router.auth.timeout_ms",
            ):
                config.pop(key)
            config.update(
                {
                    "model_providers.smart-router.name": "OpenAI",
                    "model_providers.smart-router.base_url": f"http://127.0.0.1:{router[3]}/backend-api/codex",
                    "model_providers.smart-router.model_catalog_url": f"http://127.0.0.1:{router[3]}/backend-api/codex/models",
                    "model_providers.smart-router.requires_openai_auth": True,
                }
            )
        command = [
            args.cli,
            "--no-daemon",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--strict-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(ROOT),
        ]
        for key, value in config.items():
            command.extend(["-c", key + "=" + json.dumps(value)])
        command.append("修改 Salesforce Authorization Chain。本次仅查看当前目录，然后报告结果。")
        cli_environment = {**os.environ, "SMART_ROUTER_API_KEY": router_key}
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=cli_environment,
        )
        report = {
            "kind": "local_fake_provider_only",
            "litellm_proxy": args.with_litellm,
            "credential_command": not args.account_auth,
            "account_auth": args.account_auth,
            "account_headers_observed": auth_seen if args.account_auth else None,
            "native_catalog_requests": len(catalog_calls),
            "codex_version": subprocess.check_output([args.cli, "--version"], text=True).strip(),
            "cli_exit_code": result.returncode,
            "marker_received": MARKER in result.stdout,
            "upstream_requests": len(calls),
            "received_request_encodings": received_encodings,
            "models": [call["model"] for call in calls],
            "reasoning": [call.get("reasoning", {}).get("effort") for call in calls],
            "tool_round_trip": len(calls) == 2
            and any(item.get("type") == "function_call_output" for item in calls[-1]["input"]),
            "billable_calls": 0,
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if result.returncode or not report["marker_received"] or not report["tool_round_trip"]:
            print(result.stderr[-6000:], file=sys.stderr)
            return 1
        if report["models"] != ["gpt-6-astra", "gpt-6-astra"]:
            return 1
        if args.account_auth and not catalog_calls:
            return 1
        return 0
    finally:
        for server, thread, sock, _ in reversed(servers):
            server.should_exit = True
            thread.join(timeout=3)
            sock.close()
        if gateway:
            process, log, _, _ = gateway
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            log.close()
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
