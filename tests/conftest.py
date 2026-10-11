import asyncio
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config.settings import Settings
from server.app import create_app

ROUTER_TOKEN = "test-local-router-token"
UPSTREAM_TOKEN = "test-upstream-gateway-token"
AUTH = {"Authorization": "Bearer " + ROUTER_TOKEN}


@pytest.fixture
def harness():
    @contextmanager
    def build(handler=None, **overrides):
        requests = []

        def dispatch(request):
            requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, json={"id": f"resp_{len(requests)}", "output": []})

        settings = Settings(
            **{
                "_env_file": None,
                "auth_mode": "api",
                "api_key": SecretStr(ROUTER_TOKEN),
                "upstream_api_key": SecretStr(UPSTREAM_TOKEN),
                "route_state_file": None,
                **overrides,
            }
        )
        upstream = httpx.AsyncClient(
            base_url="https://gateway.example/v1/",
            transport=httpx.MockTransport(dispatch),
        )
        app = create_app(settings, client=upstream)
        try:
            with TestClient(app) as client:
                yield client, requests, app
        finally:
            asyncio.run(upstream.aclose())

    return build
