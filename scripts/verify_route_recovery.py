"""Verify real router process restarts against a local fake upstream only."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.smoke_codex import launch  # noqa: E402


def main():
    fake = FastAPI()
    requests = []
    opaque = {"type": "reasoning", "encrypted_content": "local-test-opaque"}
    call = {"type": "function_call", "call_id": "call_restart", "name": "read", "arguments": "{}"}
    compacted = {"type": "compaction", "encrypted_content": "local-test-compact"}

    @fake.post("/v1/responses")
    async def respond(request: Request):
        payload = await request.json()
        requests.append(payload)
        return {
            "id": f"resp_{len(requests)}",
            "model": payload["model"],
            "output": [opaque, call] if len(requests) == 1 else [],
        }

    @fake.post("/v1/responses/compact")
    async def compact(request: Request):
        requests.append(await request.json())
        return {"id": "compact_restart", "output": [compacted]}

    fake_server, fake_thread, fake_socket, upstream_port = launch(fake)
    routes = []
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="route-restart-") as directory:
            state_file = Path(directory) / "routes.sqlite3"
            environment = {
                **os.environ,
                "SMART_ROUTER_AUTH_MODE": "api",
                "SMART_ROUTER_API_KEY": "local-restart-test",
                "SMART_ROUTER_UPSTREAM_API_KEY": "local-fake-upstream",
                "SMART_ROUTER_UPSTREAM_BASE_URL": f"http://127.0.0.1:{upstream_port}/v1",
                "SMART_ROUTER_ROUTE_STATE_FILE": str(state_file),
                "SMART_ROUTER_PROJECT_ROOTS": "[]",
                "SMART_ROUTER_CLASSIFIER_ENABLED": "false",
            }
            with socket.socket() as listener, (Path(directory) / "router.log").open("w") as log:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}",
                    headers={"Authorization": "Bearer local-restart-test"},
                    timeout=5,
                    trust_env=False,
                ) as client:

                    def start():
                        nonlocal process
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "uvicorn",
                                "server.app:app",
                                "--fd",
                                str(listener.fileno()),
                                "--workers",
                                "1",
                            ],
                            cwd=ROOT,
                            env=environment,
                            pass_fds=(listener.fileno(),),
                            stdout=log,
                            stderr=subprocess.STDOUT,
                        )
                        deadline = time.monotonic() + 10
                        while process.poll() is None and time.monotonic() < deadline:
                            try:
                                if client.get("/healthz", timeout=0.2).status_code == 200:
                                    return
                            except httpx.HTTPError:
                                pass
                            time.sleep(0.05)
                        raise RuntimeError("Test router failed to start")

                    def restart():
                        process.terminate()
                        process.wait(timeout=5)
                        start()

                    def send(body, endpoint="/v1/responses"):
                        response = client.post(endpoint, json={"model": "smart-auto", **body})
                        response.raise_for_status()
                        route = {
                            "status": response.status_code,
                            "model": response.headers["x-smart-router-model"],
                            "reasoning": response.headers["x-smart-router-reasoning"],
                            "source": response.headers["x-smart-router-source"],
                        }
                        routes.append(route)
                        return route

                    start()
                    original = {"role": "user", "content": "修改 CSS 按钮间距"}
                    first = send({"input": [original], "prompt_cache_key": "same-chat"})
                    restart()
                    output = {
                        "type": "function_call_output",
                        "call_id": "call_restart",
                        "output": "ok",
                    }
                    second = send(
                        {
                            "input": [output],
                            "previous_response_id": "resp_1",
                            "prompt_cache_key": "same-chat",
                        }
                    )
                    assert second["source"] == "continuation"
                    assert second["model"] == first["model"]
                    assert second["reasoning"] == first["reasoning"]
                    assert requests[-1]["input"] == [output]
                    send(
                        {"input": [original, opaque, call, output]},
                        "/v1/responses/compact",
                    )
                    assert "reasoning" not in requests[-1]
                    restart()
                    resumed = send(
                        {
                            "input": [compacted, {"role": "user", "content": "修改访问权限校验"}],
                        }
                    )
                    assert resumed["source"] == "continuation"
                    assert resumed["model"] == first["model"]
                    assert resumed["reasoning"] == first["reasoning"]
                    recovered = send(
                        {
                            "input": [
                                {"role": "user", "content": "修改访问权限校验"},
                                {"type": "reasoning", "encrypted_content": "lost-old-route"},
                                {"role": "user", "content": "修改 CSS 按钮间距"},
                            ],
                        }
                    )
                    assert recovered["source"] == "recovery"
                    assert recovered["model"] != first["model"]
                    assert len(requests) == 5  # No hidden replay/classifier calls.
                    report = {
                        "kind": "local_http_process_restart",
                        "real_model_requests": False,
                        "process_restarts": 2,
                        "checks": [
                            "same_chat_tool_continuation",
                            "compacted_history_without_original_task",
                            "previously_lost_route_recovery",
                            "no_request_replay",
                        ],
                        "routes": routes,
                    }
                    report_file = ROOT / ".run" / "route-recovery-http-report.json"
                    report_file.parent.mkdir(parents=True, exist_ok=True)
                    report_file.write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps(report, indent=2))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        fake_server.should_exit = True
        fake_thread.join(timeout=5)
        fake_socket.close()


if __name__ == "__main__":
    main()
