import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from router.route_cache import RouteCache
from router.types import RouteDecision
from tests.conftest import AUTH
from tests.test_account import ACCOUNT_AUTH
from tests.test_protocol import ChunkStream


def test_memory_expiry_and_eviction_do_not_erase_durable_routes(tmp_path, monkeypatch):
    now = 100.0
    monkeypatch.setattr("router.route_cache.time.monotonic", lambda: now)
    path = tmp_path / "routes.sqlite3"
    cache = RouteCache(1, 2, path)
    original = RouteDecision(
        model="original-model", reasoning="xhigh", source="heuristic", reason="test"
    )
    cache.put("original", original)
    original.model = "mutated"
    cache.put("other", original)
    assert "original" not in cache.entries
    now += 10000
    assert cache.get("original").model == "original-model"
    assert RouteCache(1, 2, path).get("original").reasoning == "xhigh"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("streaming", [False, True])
def test_response_and_tool_chain_survive_restart(harness, tmp_path, streaming):
    path = tmp_path / "routes.sqlite3"
    task = {"role": "user", "content": "修改 CSS 按钮间距"}
    call = {"type": "function_call", "call_id": "call_restart", "name": "read", "arguments": "{}"}
    response = {"id": "resp_restart", "output": [call]}
    wire = (
        "event: response.created\ndata: "
        + json.dumps({"type": "response.created", "response": response})
        + "\n\n"
    ).encode()

    def handler(_):
        if streaming:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([wire])
            )
        return httpx.Response(200, json=response)

    with harness(handler, route_state_file=path) as (client, _, app):
        first = client.post(
            "/v1/responses",
            headers=AUTH,
            json={"model": "smart-auto", "input": [task], "stream": streaming},
        )
        assert first.status_code == 200
        selected = first.headers["x-smart-router-model"]
        effort = first.headers["x-smart-router-reasoning"]
        assert selected == app.state.router.engine.tiers.simple.model
    # Both response-id and tool-only continuation work in a new service instance.
    for previous in ["resp_restart", None]:
        with harness(route_state_file=path) as (client, requests, _):
            body = {
                "model": "smart-auto",
                "input": [
                    {"type": "function_call_output", "call_id": "call_restart", "output": "ok"}
                ],
            }
            if previous:
                body["previous_response_id"] = previous
            followup = client.post("/v1/responses", headers=AUTH, json=body)
            assert followup.status_code == 200
            assert followup.headers["x-smart-router-source"] == "continuation"
            forwarded = json.loads(requests[0].content)
            assert forwarded["model"] == selected
            assert forwarded["reasoning"]["effort"] == effort
            assert forwarded["input"] == body["input"]


@pytest.mark.parametrize("session_header", ["X-Codex-Thread-ID", "session_id", "prompt_cache_key"])
def test_compaction_session_survives_restart_and_directory_change(
    harness, tmp_path, session_header
):
    path = tmp_path / "routes.sqlite3"
    headers = dict(AUTH)
    body = {"model": "smart-auto", "input": "修改 CSS 按钮间距"}
    if session_header == "prompt_cache_key":
        body[session_header] = "session-test"
    else:
        headers[session_header] = "session-test"
    with harness(route_state_file=path) as (client, _, _):
        first = client.post("/v1/responses", headers=headers, json=body)
        assert first.status_code == 200
    body["input"] = [
        {
            "role": "user",
            "content": "<environment_context><cwd>/different</cwd></environment_context>",
        },
        {"type": "compaction", "encrypted_content": "not-seen-before"},
        {"role": "user", "content": "继续"},
    ]
    with harness(route_state_file=path) as (client, requests, _):
        followup = client.post("/v1/responses", headers=headers, json=body)
        assert followup.status_code == 200
        assert followup.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[0].content)["model"] == first.headers["x-smart-router-model"]


def test_compaction_output_recovers_route_without_original_task_or_session(harness, tmp_path):
    path = tmp_path / "routes.sqlite3"
    compacted = {"type": "compaction", "encrypted_content": "private-compact-history"}
    original = {"role": "user", "content": "修改 CSS 按钮间距"}
    with harness(
        lambda _: httpx.Response(200, json={"id": "compact_1", "output": [compacted]}),
        route_state_file=path,
    ) as (client, _, _):
        first = client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        compact = client.post(
            "/v1/responses/compact",
            headers=AUTH,
            json={"model": "smart-auto", "input": [original]},
        )
        assert compact.status_code == 200
    with harness(route_state_file=path) as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [compacted, {"role": "user", "content": "继续"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[0].content)["model"] == first.headers["x-smart-router-model"]


def test_old_unrecorded_transcript_recovers_from_original_task_and_pins_it(harness, tmp_path):
    path = tmp_path / "routes.sqlite3"
    original = {"role": "user", "content": "修改访问权限校验"}
    opaque = {"type": "reasoning", "encrypted_content": "private-encrypted-history"}
    output = {"type": "function_call_output", "call_id": "call_old", "output": "css-only"}
    followup = {"role": "user", "content": "修改 CSS 按钮间距"}
    body = {"model": "smart-auto", "input": [original, opaque, followup, output]}
    with harness(route_state_file=path, classifier_enabled=True) as (client, requests, app):
        response = client.post("/v1/responses", headers=AUTH, json=body)
        assert response.status_code == 200
        assert response.headers["x-smart-router-source"] == "recovery"
        assert len(requests) == 1  # No classifier inference or request replay.
        forwarded = json.loads(requests[0].content)
        assert forwarded["input"] == body["input"]
        assert forwarded["model"] == app.state.router.engine.tiers.high_risk.model
        assert forwarded["reasoning"]["effort"] == app.state.router.engine.tiers.high_risk.reasoning
    with harness(route_state_file=path) as (client, _, _):
        body["input"] = [followup, opaque, output]  # Original task no longer supplied.
        response = client.post("/v1/responses", headers=AUTH, json=body)
        assert response.status_code == 200
        assert response.headers["x-smart-router-source"] == "continuation"
        assert response.headers["x-smart-router-reasoning"] == forwarded["reasoning"]["effort"]
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT key, decision FROM routes").fetchall()
    stored = json.dumps(rows)
    for private in [
        original["content"],
        opaque["encrypted_content"],
        output["output"],
        AUTH["Authorization"],
    ]:
        assert private not in stored


def test_route_state_is_account_scoped_and_survives_token_refresh(harness, tmp_path):
    path = tmp_path / "routes.sqlite3"
    session = {"session_id": "shared-session"}
    body = {"model": "smart-auto", "input": "修改 CSS 按钮间距"}
    with harness(auth_mode="chatgpt", route_state_file=path) as (client, _, _):
        first = client.post(
            "/backend-api/codex/responses", headers={**ACCOUNT_AUTH, **session}, json=body
        )
        assert first.status_code == 200
    body["input"] = [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"role": "user", "content": "继续"},
    ]
    with harness(auth_mode="chatgpt", route_state_file=path) as (client, requests, _):
        refreshed = {**ACCOUNT_AUTH, **session, "Authorization": "Bearer refreshed-token"}
        continuation = client.post("/backend-api/codex/responses", headers=refreshed, json=body)
        assert continuation.status_code == 200
        assert continuation.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[-1].content)["model"] == first.headers["x-smart-router-model"]
        other = {**ACCOUNT_AUTH, **session, "ChatGPT-Account-ID": "different-account"}
        body["input"] = [
            {"role": "user", "content": "修改访问权限校验"},
            {"type": "reasoning", "encrypted_content": "different-account-history"},
        ]
        recovered = client.post("/backend-api/codex/responses", headers=other, json=body)
        assert recovered.status_code == 200
        assert recovered.headers["x-smart-router-source"] == "recovery"
        assert json.loads(requests[-1].content)["model"] != first.headers["x-smart-router-model"]
    assert ACCOUNT_AUTH["Authorization"].encode() not in path.read_bytes()
    assert ACCOUNT_AUTH["ChatGPT-Account-ID"].encode() not in path.read_bytes()


def test_manual_to_auto_continuation_does_not_send_empty_reasoning(harness, tmp_path):
    path = tmp_path / "routes.sqlite3"
    opaque = {"type": "reasoning", "encrypted_content": "manual-history"}
    with harness(auth_mode="chatgpt", route_state_file=path) as (client, _, _):
        first = client.post(
            "/backend-api/codex/responses",
            headers=ACCOUNT_AUTH,
            json={"model": "gpt-6-luna", "input": [opaque]},
        )
        assert first.status_code == 200
    with harness(auth_mode="chatgpt", route_state_file=path) as (client, requests, _):
        response = client.post(
            "/backend-api/codex/responses",
            headers=ACCOUNT_AUTH,
            json={"model": "smart-auto", "input": [opaque]},
        )
        assert response.status_code == 200
        assert response.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[0].content) == {"model": "gpt-6-luna", "input": [opaque]}


def test_missing_upstream_response_is_not_hidden_or_replayed(harness):
    with harness(lambda _: httpx.Response(400, json={"error": "unknown response"})) as (
        client,
        requests,
        _,
    ):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={"model": "smart-auto", "input": [], "previous_response_id": "gone"},
        )
        assert response.status_code == 400
        assert response.json() == {"error": "unknown response"}
        assert len(requests) == 1


def test_followup_text_does_not_pin_an_unrelated_old_conversation(harness, tmp_path):
    with harness(route_state_file=tmp_path / "routes.sqlite3") as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={"model": "smart-auto", "input": "修改 CSS 按钮间距"},
        )
        assert response.status_code == 200
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "previous_response_id": "resp_1",
                "input": [
                    {"type": "compaction", "encrypted_content": "compact-small-task"},
                    {"role": "user", "content": "修改访问权限校验"},
                ],
            },
        )
        assert response.status_code == 200
        assert json.loads(requests[-1].content)["model"] == "gpt-6.1-sol"
        unrelated = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [
                    {"role": "user", "content": "修改访问权限校验"},
                    {"role": "user", "content": "修改 CSS 按钮间距"},
                    {"type": "reasoning", "encrypted_content": "unrelated-history"},
                ],
            },
        )
        assert unrelated.status_code == 200
        assert unrelated.headers["x-smart-router-source"] == "recovery"
        assert json.loads(requests[-1].content)["model"] == "gpt-6.1-sol"
