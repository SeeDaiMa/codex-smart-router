import json

import httpx
import pytest
import zstandard
from pydantic import ValidationError

from config.settings import Settings
from tests.conftest import AUTH

ACCOUNT_AUTH = {
    "Authorization": "Bearer test-codex-managed-account-token",
    "ChatGPT-Account-ID": "test-account",
}


def test_account_auto_needs_no_api_key_and_keeps_account_headers(harness):
    with harness(auth_mode="chatgpt", upstream_api_key=None) as (client, requests, app):
        response = client.post(
            "/backend-api/codex/responses",
            headers={
                **ACCOUNT_AUTH,
                "X-Gateway-Key": "private-local-secret",
                "X-Codex-Turn-ID": "test-turn",
                "Cookie": "private-session",
                "Originator": "codex_cli_rs",
                "Accept": "text/event-stream",
            },
            json={"model": "smart-auto", "input": "修改访问权限校验"},
        )
        assert response.status_code == 200
        forwarded = json.loads(requests[0].content)
        assert forwarded["model"] == "gpt-6.1-sol"
        assert forwarded["reasoning"]["effort"] == app.state.router.engine.tiers.high_risk.reasoning
        assert requests[0].headers["authorization"] == ACCOUNT_AUTH["Authorization"]
        assert requests[0].headers["chatgpt-account-id"] == "test-account"
        assert requests[0].headers["x-codex-turn-id"] == "test-turn"
        assert requests[0].headers["originator"] == "codex_cli_rs"
        assert requests[0].headers["accept"] == "text/event-stream"
        assert "x-gateway-key" not in requests[0].headers
        assert "cookie" not in requests[0].headers


@pytest.mark.parametrize("effort", [None, "high", "ultra"])
def test_manual_selection_passes_through_without_local_chain_state(harness, effort):
    body = {
        "model": "gpt-6.1-sol",
        "input": [{"type": "reasoning", "encrypted_content": "opaque"}],
        "previous_response_id": "response-before-router-restart",
        "store": False,
    }
    if effort:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
    with harness(auth_mode="chatgpt", upstream_api_key=None) as (client, requests, _):
        response = client.post("/backend-api/codex/responses", headers=ACCOUNT_AUTH, json=body)
        assert response.status_code == 200
        assert json.loads(requests[0].content) == body
        assert response.headers["x-smart-router-source"] == "manual"


def test_native_catalog_preserves_models_and_adds_auto(harness):
    original = {
        "models": [
            {
                "slug": "gpt-6.1-sol",
                "display_name": "Sol",
                "use_responses_lite": True,
                "base_instructions": "Native Codex instructions",
            },
            {"slug": "gpt-5.6-sol", "base_instructions": "Legacy instructions"},
        ],
        "catalog_version": "test",
    }
    with harness(lambda _: httpx.Response(200, json=original), auth_mode="chatgpt") as (
        client,
        requests,
        _,
    ):
        response = client.get(
            "/backend-api/codex/models?client_version=0.162.1", headers=ACCOUNT_AUTH
        )
        assert response.status_code == 200
        catalog = response.json()
        assert [model["slug"] for model in catalog["models"]] == [
            "smart-auto",
            "gpt-6.1-sol",
            "gpt-5.6-sol",
        ]
        assert catalog["models"][0]["display_name"] == "⭐ Auto"
        assert catalog["models"][0]["base_instructions"] == "Native Codex instructions"
        assert all(not model["use_responses_lite"] for model in catalog["models"])
        assert catalog["catalog_version"] == "test"
        assert requests[0].url.params["client_version"] == "0.162.1"
        assert requests[0].headers["authorization"] == ACCOUNT_AUTH["Authorization"]


def test_account_auth_and_local_preview_use_separate_credentials(harness):
    with harness(auth_mode="chatgpt") as (client, requests, _):
        assert client.get("/backend-api/codex/models").status_code == 401
        assert (
            client.post("/v1/route", headers=ACCOUNT_AUTH, json={"task": "修改 CSS"}).status_code
            == 401
        )
        assert client.post("/v1/route", headers=AUTH, json={"task": "修改 CSS"}).status_code == 200
        assert not requests


@pytest.mark.parametrize(
    ("luna_metadata", "expected_max", "expected_percent"),
    [
        (
            {
                "context_window": 272000,
                "max_context_window": 872000,
                "effective_context_window_percent": 95,
            },
            872000,
            95,
        ),
        (
            {
                "context_window": 272000,
                "max_context_window": 400000,
                "effective_context_window_percent": 90,
            },
            400000,
            90,
        ),
        ({"context_window": 272000}, 272000, 95),
        (None, 272000, 95),
    ],
)
def test_auto_context_respects_all_configured_models(
    harness, luna_metadata, expected_max, expected_percent
):
    sol = {
        "slug": "gpt-6.1-sol",
        "context_window": 272000,
        "max_context_window": 872000,
        "effective_context_window_percent": 95,
    }
    models = [sol]
    if luna_metadata is not None:
        models.append({"slug": "gpt-6-luna", **luna_metadata})
    # A smaller manual-only model must not constrain Auto.
    models.append({"slug": "gpt-5.5", "context_window": 128000})
    with harness(
        lambda _: httpx.Response(200, json={"models": models}), auth_mode="chatgpt"
    ) as (client, _, _):
        catalog = client.get("/backend-api/codex/models", headers=ACCOUNT_AUTH).json()
        auto = catalog["models"][0]
        assert auto["context_window"] == 272000
        assert auto["max_context_window"] == expected_max
        assert auto["effective_context_window_percent"] == expected_percent
        assert catalog["models"][1]["max_context_window"] == sol["max_context_window"]


def test_backend_account_error_and_quota_headers_are_preserved(harness):
    error = {"error": {"message": "account limit reached"}}
    with harness(
        lambda _: httpx.Response(
            429, json=error, headers={"retry-after": "5", "x-codex-primary-used-percent": "100"}
        ),
        auth_mode="chatgpt",
    ) as (client, requests, _):
        response = client.post(
            "/backend-api/codex/responses",
            headers=ACCOUNT_AUTH,
            json={"model": "smart-auto", "input": "新增页面"},
        )
        assert response.status_code == 429
        assert response.json() == error
        assert response.headers["x-codex-primary-used-percent"] == "100"
        assert response.headers["retry-after"] == "5"
        assert len(requests) == 1


@pytest.mark.parametrize("result", [{"data": []}, {"models": "invalid"}, []])
def test_invalid_account_catalog_has_clear_error(harness, result):
    with harness(lambda _: httpx.Response(200, json=result), auth_mode="chatgpt") as (
        client,
        _,
        _,
    ):
        response = client.get("/backend-api/codex/models", headers=ACCOUNT_AUTH)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "invalid_model_catalog"


def test_chatgpt_destination_cannot_be_an_arbitrary_third_party():
    with pytest.raises(ValidationError, match="official Codex backend"):
        Settings(_env_file=None, chatgpt_base_url="https://gateway.example/backend-api/codex")


def test_native_codex_zstd_requests_are_decoded_before_routing(harness):
    payload = {"model": "smart-auto", "input": "修改 CSS 按钮间距", "store": False}
    compressed = zstandard.ZstdCompressor().compress(json.dumps(payload).encode())
    with harness(auth_mode="chatgpt") as (client, requests, _):
        response = client.post(
            "/backend-api/codex/responses",
            headers={**ACCOUNT_AUTH, "Content-Encoding": "zstd"},
            content=compressed,
        )
        assert response.status_code == 200
        assert json.loads(requests[0].content)["input"] == payload["input"]
        assert "content-encoding" not in requests[0].headers


def test_decoded_request_size_is_bounded(harness):
    payload = {"model": "smart-auto", "input": "x" * 100000}
    compressed = zstandard.ZstdCompressor().compress(json.dumps(payload).encode())
    with harness(auth_mode="chatgpt", max_body_bytes=1024) as (client, requests, _):
        response = client.post(
            "/backend-api/codex/responses",
            headers={**ACCOUNT_AUTH, "Content-Encoding": "zstd"},
            content=compressed,
        )
        assert response.status_code == 413
        assert not requests


def test_account_stream_without_mime_keeps_native_sse(harness):
    wire = (
        b'event: response.completed\ndata: {"type":"response.completed",'
        b'"response":{"id":"resp_native","output":[]}}\n\n'
    )
    with harness(lambda _: httpx.Response(200, content=wire), auth_mode="chatgpt") as (
        client,
        requests,
        _,
    ):
        response = client.post(
            "/backend-api/codex/responses",
            headers=ACCOUNT_AUTH,
            json={"model": "smart-auto", "input": "新增页面", "stream": True},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert response.content == wire
        assert len(requests) == 1


def test_catalog_cache_is_scoped_and_does_not_retain_credentials(harness):
    with harness(lambda _: httpx.Response(200, json={"models": []}), auth_mode="chatgpt") as (
        client,
        requests,
        app,
    ):
        for _ in range(2):
            assert client.get("/backend-api/codex/models", headers=ACCOUNT_AUTH).status_code == 200
        assert len(requests) == 1
        other = {**ACCOUNT_AUTH, "Authorization": "Bearer other-account-token"}
        assert client.get("/backend-api/codex/models", headers=other).status_code == 200
        assert len(requests) == 2
        entries = repr(app.state.router.account_catalog.entries)
        assert "test-codex-managed-account-token" not in entries
        assert "other-account-token" not in entries


def test_catalog_errors_are_not_cached(harness):
    with harness(lambda _: httpx.Response(401, json={"error": "expired"}), auth_mode="chatgpt") as (
        client,
        requests,
        _,
    ):
        for _ in range(2):
            assert client.get("/backend-api/codex/models", headers=ACCOUNT_AUTH).status_code == 401
        assert len(requests) == 2
