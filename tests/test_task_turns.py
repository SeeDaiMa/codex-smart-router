import json
import subprocess

import httpx
import pytest

from tests.conftest import AUTH
from tests.test_account import ACCOUNT_AUTH


@pytest.mark.parametrize("auth_mode", ["api", "chatgpt"])
@pytest.mark.parametrize("use_previous", [False, True])
def test_new_task_switches_model_and_tools_keep_the_new_route(
    harness, tmp_path, auth_mode, use_previous
):
    path = tmp_path / "routes.sqlite3"
    headers = {
        **(ACCOUNT_AUTH if auth_mode == "chatgpt" else AUTH),
        "session_id": "switch-task",
    }
    endpoint = "/backend-api/codex/responses" if auth_mode == "chatgpt" else "/v1/responses"
    original = {"role": "user", "content": "新增查询接口"}
    old_history = {"type": "reasoning", "encrypted_content": "old-sol-history"}
    commit = {"role": "user", "content": "提交 Git"}
    new_history = {"type": "reasoning", "encrypted_content": "new-luna-history"}
    call = {"type": "function_call", "call_id": "commit-call", "name": "exec", "arguments": "{}"}
    tool = {"type": "function_call_output", "call_id": "commit-call", "output": "ok"}

    with harness(
        lambda _: httpx.Response(200, json={"id": "old-response", "output": [old_history]}),
        auth_mode=auth_mode,
        route_state_file=path,
    ) as (client, _, _):
        first = client.post(
            endpoint, headers=headers, json={"model": "smart-auto", "input": [original]}
        )
        assert first.headers["x-smart-router-model"] == "gpt-6.1-sol"
        assert first.headers["x-smart-router-reasoning"] == "high"
    body = {"model": "smart-auto", "input": [original, old_history, commit]}
    if use_previous:
        body["previous_response_id"] = "old-response"
    with harness(
        lambda _: httpx.Response(200, json={"id": "new-response", "output": [new_history, call]}),
        auth_mode=auth_mode,
        route_state_file=path,
    ) as (client, requests, _):
        second = client.post(endpoint, headers=headers, json=body)
        assert second.headers["x-smart-router-model"] == "gpt-6-luna"
        assert second.headers["x-smart-router-reasoning"] == "xhigh"
        forwarded = json.loads(requests[-1].content)
        assert forwarded["input"] == body["input"]
        if use_previous:
            assert forwarded["previous_response_id"] == "old-response"

    with harness(auth_mode=auth_mode, route_state_file=path) as (client, requests, _):
        body["input"].extend([new_history, call, tool])
        # Even a client carrying the old response ID must use the current turn
        # when its full transcript identifies the newly selected task.
        third = client.post(endpoint, headers=headers, json=body)
        assert third.headers["x-smart-router-source"] == "continuation"
        assert third.headers["x-smart-router-model"] == "gpt-6-luna"
        assert json.loads(requests[-1].content)["input"] == body["input"]

        body["input"].append({"role": "user", "content": "修改访问权限校验"})
        fourth = client.post(endpoint, headers=headers, json=body)
        assert fourth.headers["x-smart-router-model"] == "gpt-6.1-sol"
        assert fourth.headers["x-smart-router-reasoning"] == "xhigh"

        # Rerouting must not rewrite the historical Sol route: an older branch
        # still has its original model even after this session changed twice.
        older = client.post(
            endpoint,
            headers=headers,
            json={"model": "smart-auto", "input": [original, old_history]},
        )
        assert older.headers["x-smart-router-model"] == "gpt-6.1-sol"
        assert older.headers["x-smart-router-reasoning"] == "high"


def test_pending_tool_call_is_not_a_task_switch(harness):
    call = {"type": "function_call", "call_id": "pending", "name": "read", "arguments": "{}"}
    original = {"role": "user", "content": "修改访问权限"}
    with harness(
        lambda _: httpx.Response(200, json={"id": "pending-response", "output": [call]})
    ) as (client, requests, _):
        client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        body = {
            "model": "smart-auto",
            "input": [original, call, {"role": "user", "content": "提交 Git"}],
        }
        response = client.post("/v1/responses", headers=AUTH, json=body)
        assert response.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[-1].content)["model"] == "gpt-6.1-sol"
        assert json.loads(requests[-1].content)["input"] == body["input"]


@pytest.mark.parametrize("followup", ["继续", "可以", "好的", "按你说的做"])
def test_short_approval_keeps_session_route(harness, followup):
    headers = {**AUTH, "session_id": "approval"}
    with harness() as (client, _, _):
        first = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": "smart-auto", "input": "设计系统架构"},
        )
        response = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": "smart-auto", "input": followup},
        )
        assert response.headers["x-smart-router-source"] == "continuation"
        assert (
            response.headers["x-smart-router-reasoning"]
            == first.headers["x-smart-router-reasoning"]
        )


def test_tool_output_cannot_downgrade_the_route(harness):
    with harness() as (client, _, _):
        original = {"role": "user", "content": "设计系统架构"}
        client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [
                    original,
                    {"type": "function_call_output", "call_id": "read", "output": "提交 Git"},
                ],
            },
        )
        assert response.headers["x-smart-router-model"] == "gpt-6.1-sol"
        assert response.headers["x-smart-router-reasoning"] == "max"


def test_compact_uses_the_selected_turn_without_reclassifying(harness):
    original = {"role": "user", "content": "设计系统架构"}
    transcript = [original, {"role": "assistant", "content": "Done"}]
    with harness() as (client, requests, _):
        client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        transcript.append({"role": "user", "content": "提交 Git"})
        selected = client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": transcript}
        )
        assert selected.headers["x-smart-router-model"] == "gpt-6-luna"
        response = client.post(
            "/v1/responses/compact",
            headers=AUTH,
            json={"model": "smart-auto", "input": transcript},
        )
        assert response.headers["x-smart-router-source"] == "continuation"
        assert json.loads(requests[-1].content)["model"] == "gpt-6-luna"
        assert "reasoning" not in json.loads(requests[-1].content)


def test_plain_assistant_history_continues_the_selected_turn(harness):
    transcript = [
        {"role": "user", "content": "设计系统架构"},
        {"role": "assistant", "content": "Done"},
        {"role": "user", "content": "提交 Git"},
    ]
    with harness() as (client, _, _):
        first = client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": transcript}
        )
        transcript.append({"role": "assistant", "content": "Committed"})
        response = client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": transcript}
        )
        assert first.headers["x-smart-router-model"] == "gpt-6-luna"
        assert response.headers["x-smart-router-source"] == "continuation"
        assert response.headers["x-smart-router-model"] == "gpt-6-luna"


def test_repeated_task_text_in_a_different_turn_does_not_reuse_old_project_analysis(
    harness, tmp_path
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for index in range(20):
        (tmp_path / f"{index}.txt").write_text("test")
    original = {"role": "user", "content": "提交 Git"}
    with harness(project_roots=[tmp_path]) as (client, _, _):
        body = {
            "model": "smart-auto",
            "metadata": {"smart_router_project_root": str(tmp_path)},
            "input": [original],
        }
        first = client.post("/v1/responses", headers=AUTH, json=body)
        assert first.headers["x-smart-router-model"] == "gpt-6.1-sol"
        for file in tmp_path.glob("*.txt"):
            file.unlink()
        body["input"].extend(
            [{"role": "assistant", "content": "Done"}, {"role": "user", "content": "提交 Git"}]
        )
        second = client.post("/v1/responses", headers=AUTH, json=body)
        assert second.headers["x-smart-router-model"] == "gpt-6-luna"
