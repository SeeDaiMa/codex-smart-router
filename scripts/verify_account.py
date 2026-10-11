"""Verify the current Codex account through a temporary local router.

By default, only discover the account model catalog. --invoke also makes one
small real model request, using the current account's Codex allowance. Never
read the credential store or write user configuration.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from providers.upstream import SSEObserver  # noqa: E402
from scripts.smoke_codex import launch  # noqa: E402
from server.app import create_app  # noqa: E402

MARKER = "SMART_ROUTER_ACCOUNT_OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="codex")
    parser.add_argument("--invoke", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / ".run" / "live-account-report.json")
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"kind": "official_codex_account", "real_model_request_enabled": args.invoke}
    routes = []
    catalog_requests = []
    responses = []
    upstream_metadata = []
    hooks_added = False
    app = create_app(
        Settings(
            _env_file=None,
            auth_mode="chatgpt",
            classifier_enabled=False,
            project_roots=[],
            upstream_timeout_seconds=45,
        )
    )

    @app.middleware("http")
    async def observe(request, call_next):
        nonlocal hooks_added
        if not hooks_added:

            async def record_upstream(value):
                upstream_metadata.append(
                    {
                        "endpoint": value.request.url.path.rsplit("/", 1)[-1],
                        "status": value.status_code,
                        "content_type": value.headers.get("content-type"),
                    }
                )

            request.app.state.router.client.event_hooks["response"].append(record_upstream)
            hooks_added = True
        catalog_request = None
        if request.method == "GET" and request.url.path.endswith("/models"):
            catalog_request = {"status": "pending"}
            catalog_requests.append(catalog_request)
        response = await call_next(request)
        if catalog_request is not None:
            catalog_request["status"] = response.status_code
            catalog_request["response_bytes"] = response.headers.get("content-length")
        if request.method == "POST" and request.url.path.endswith("/responses"):
            routes.append(
                {
                    "status": response.status_code,
                    "model": response.headers.get("x-smart-router-model"),
                    "reasoning": response.headers.get("x-smart-router-reasoning"),
                    "source": response.headers.get("x-smart-router-source"),
                }
            )
            if "text/event-stream" in response.headers.get("content-type", ""):

                def record(value):
                    if value.get("status") in {"completed", "failed", "incomplete"}:
                        usage = value.get("usage") or {}
                        responses.append(
                            {
                                "model": value.get("model"),
                                "status": value.get("status"),
                                "usage": {
                                    key: usage[key]
                                    for key in ("input_tokens", "output_tokens", "total_tokens")
                                    if key in usage
                                },
                            }
                        )

                original = response.body_iterator

                async def observed_stream():
                    observer = SSEObserver(record)
                    async for chunk in original:
                        observer.feed(chunk)
                        yield chunk

                response.body_iterator = observed_stream()
        return response

    server, thread, sock, port = launch(app)
    try:
        config = {
            "model": "smart-auto",
            "model_provider": "smart-router",
            "model_reasoning_effort": "medium",
            "model_providers.smart-router.name": "OpenAI",
            "model_providers.smart-router.base_url": f"http://127.0.0.1:{port}/backend-api/codex",
            "model_providers.smart-router.model_catalog_url": f"http://127.0.0.1:{port}/backend-api/codex/models",
            "model_providers.smart-router.wire_api": "responses",
            "model_providers.smart-router.requires_openai_auth": True,
            "model_providers.smart-router.supports_websockets": False,
            "model_providers.smart-router.request_max_retries": 0,
            "model_providers.smart-router.stream_max_retries": 0,
            "sqlite_home": str(args.report.parent / "codex-account-state"),
            "log_dir": str(args.report.parent / "codex-account-logs"),
        }
        overrides = [
            arg for key, value in config.items() for arg in ("-c", key + "=" + json.dumps(value))
        ]
        slugs = []
        # Catalog discovery has a short native deadline. A slow first download
        # can finish in the router while Codex has already given up. Retry this
        # read-only discovery up to twice, letting the scoped cache serve it.
        # Inference is submitted only once and is never automatically replayed.
        for attempt in range(3):
            discovered = subprocess.run(
                [args.cli, "--no-daemon", "debug", "models", *overrides],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            report["catalog_attempts"] = attempt + 1
            if not discovered.returncode:
                catalog = json.loads(discovered.stdout)
                models = catalog["models"] if isinstance(catalog, dict) else catalog
                slugs = [model["slug"] for model in models]
            if "smart-auto" in slugs:
                break
        report["catalog_cli_exit_code"] = discovered.returncode
        report["gateway_catalog_requests"] = catalog_requests
        if discovered.returncode:
            report["catalog_error"] = "Codex could not discover the gateway model catalog"
            return 1
        report["auto_in_catalog"] = "smart-auto" in slugs
        report["account_models_preserved"] = "gpt-6.1-sol" in slugs
        report["routing_models_available"] = {
            model: model in slugs for model in app.state.router.engine.models
        }
        if not report["auto_in_catalog"]:
            report["catalog_error"] = "The refreshed catalog did not include Auto"
            print(
                discovered.stderr[-3000:],
                file=sys.stderr,
            )
            return 1
        if args.invoke:
            result = subprocess.run(
                [
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
                    *overrides,
                    f"只回复 {MARKER}。不要调用工具，不要读取或修改文件。",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            report["inference_cli_exit_code"] = result.returncode
            report["marker_received"] = MARKER in result.stdout
            report["routes"] = routes
            report["upstream_responses"] = responses
            if result.returncode or not report["marker_received"]:
                report["inference_error"] = "Account inference did not complete successfully"
                print(
                    "\n".join(
                        line for line in result.stderr.splitlines() if line.startswith("ERROR:")
                    )[-2000:],
                    file=sys.stderr,
                )
                return 1
        return 0
    except (ValueError, subprocess.TimeoutExpired) as exc:
        report["error_type"] = type(exc).__name__
        return 1
    finally:
        report["upstream_metadata"] = upstream_metadata
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
