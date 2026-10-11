import json

import httpx
import pytest

from providers.upstream import SSEObserver
from tests.conftest import AUTH, UPSTREAM_TOKEN


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("test disconnect")

    async def aclose(self):
        self.closed = True


def test_authentication_precedes_provider_calls(harness):
    with harness() as (client, requests, _):
        assert client.get("/healthz").status_code == 200
        for headers in [{}, {"Authorization": "Bearer wrong"}]:
            response = client.post(
                "/v1/responses",
                headers=headers,
                json={
                    "model": "smart-auto",
                    "input": "修改 CSS",
                },
            )
            assert response.status_code == 401
        assert client.get("/v1/models", headers=AUTH).json()["data"][0]["id"] == "smart-auto"
        assert requests == []


def test_full_responses_payload_is_preserved_and_credentials_are_separate(harness):
    body = {
        "model": "smart-auto",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "修改权限校验"},
                    {"type": "input_image", "image_url": "data:image/png;base64,dGVzdA=="},
                ],
            }
        ],
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
        "instructions": "Workspace instructions",
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "low", "summary": "auto"},
        "text": {"verbosity": "low"},
        "metadata": {"smart_router_project_root": "/unscanned", "owner": "example"},
    }
    with harness() as (client, requests, _):
        response = client.post(
            "/v1/responses", headers={**AUTH, "OpenAI-Organization": "private"}, json=body
        )
        assert response.status_code == 200
        forwarded = json.loads(requests[0].content)
        assert forwarded["model"] == "gpt-6-astra"
        assert forwarded["reasoning"] == {"effort": "high", "summary": "auto"}
        assert forwarded["metadata"] == {"owner": "example"}
        for key in ("input", "tools", "include", "store", "instructions", "text"):
            assert forwarded[key] == body[key]
        assert requests[0].headers["authorization"] == "Bearer " + UPSTREAM_TOKEN
        assert "openai-organization" not in requests[0].headers
        assert response.headers["x-smart-router-model"] == "gpt-6-astra"


def test_codex_environment_and_agents_are_not_the_task(harness):
    with harness() as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [
                    {
                        "role": "user",
                        "content": "# AGENTS.md instructions\nPay attention to security",
                    },
                    {
                        "role": "user",
                        "content": (
                            "<environment_context><cwd>/unscanned</cwd></environment_context>"
                        ),
                    },
                    {"role": "user", "content": "修改 CSS 按钮间距"},
                ],
            },
        )
        assert response.status_code == 200
        assert json.loads(requests[0].content)["model"] == "gpt-5.6-terra"


def test_sse_is_byte_exact_and_continuation_uses_pinned_model(harness):
    wire = (
        'event: response.created\r\ndata: {"type":"response.created",'
        '"response":{"id":"resp_sse"}}\r\n\r\n'
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
        '"delta":"中文"}\n\n'
        'event: response.completed\ndata: {"type":"response.completed",'
        '"response":{"id":"resp_sse","output":[]}}\n\n'
    ).encode()
    upstream_stream = ChunkStream([wire[index : index + 3] for index in range(0, len(wire), 3)])

    def handler(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=upstream_stream
            )
        return httpx.Response(200, json={"id": "resp_next", "output": []})

    with harness(handler) as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": "修改权限校验",
                "stream": True,
            },
        )
        assert response.content == wire
        assert upstream_stream.closed
        output = {"type": "function_call_output", "call_id": "call_1", "output": "CSS file read"}
        followup = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [output],
                "previous_response_id": "resp_sse",
            },
        )
        assert followup.status_code == 200
        assert json.loads(requests[-1].content)["model"] == "gpt-6-astra"
        assert json.loads(requests[-1].content)["input"] == [output]


def test_full_transcript_continues_without_reclassifying_tool_output(harness):
    original = {"role": "user", "content": "修改权限校验"}
    with harness() as (client, requests, _):
        client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        transcript = [
            original,
            {"type": "reasoning", "encrypted_content": "opaque"},
            {"type": "function_call_output", "call_id": "c1", "output": "css style only"},
        ]
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": transcript,
            },
        )
        assert response.status_code == 200
        assert json.loads(requests[-1].content)["model"] == "gpt-6-astra"
        assert response.headers["x-smart-router-source"] == "continuation"
        transcript.append({"role": "user", "content": "继续完善页面"})
        assert (
            client.post(
                "/v1/responses",
                headers=AUTH,
                json={
                    "model": "smart-auto",
                    "input": transcript,
                },
            ).status_code
            == 200
        )


def test_unknown_continuation_fails_before_upstream(harness):
    with harness() as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [],
                "previous_response_id": "unknown",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "routing_state_missing"
        assert not requests


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_provider_errors_keep_status_body_and_retry_after(harness, status):
    wire = b'{"error":{"type":"upstream_error","message":"test"}}'
    with harness(
        lambda _: httpx.Response(
            status,
            content=wire,
            headers={
                "content-type": "application/json",
                "retry-after": "7",
                "x-request-id": "req_test",
            },
        )
    ) as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": "新增查询接口",
                "stream": True,
            },
        )
        assert response.status_code == status
        assert response.content == wire
        assert response.headers["retry-after"] == "7"
        assert len(requests) == 1


def test_stream_disconnect_reports_error_without_replay(harness):
    upstream_stream = ChunkStream([b"event: response.created\ndata: {}\n\n"], fail=True)
    with harness(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=upstream_stream
        )
    ) as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": "新增接口",
                "stream": True,
            },
        )
        assert b"upstream_stream_error" in response.content
        assert upstream_stream.closed
        assert len(requests) == 1


def test_compaction_keeps_model_without_injecting_reasoning(harness):
    original = {"role": "user", "content": "修改 CSS"}
    with harness() as (client, requests, _):
        client.post(
            "/v1/responses", headers=AUTH, json={"model": "smart-auto", "input": [original]}
        )
        response = client.post(
            "/v1/responses/compact",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": [original, {"type": "reasoning", "encrypted_content": "opaque"}],
                "instructions": "compact context",
            },
        )
        assert response.status_code == 200
        assert requests[-1].url.path == "/v1/responses/compact"
        assert json.loads(requests[-1].content)["model"] == "gpt-5.6-terra"
        assert "reasoning" not in json.loads(requests[-1].content)


@pytest.mark.parametrize(
    "body,status",
    [
        (b"broken", 400),
        (b"[]", 400),
        (b'{"model":"smart-auto","input":1}', 400),
        (b'{"model":"other","input":"test"}', 404),
        (b'{"model":"smart-auto","input":"test","stream":"true"}', 400),
        (b'{"model":"smart-auto","input":"test","reasoning":"high"}', 400),
        (b'{"model":"smart-auto","input":"test","temperature":NaN}', 400),
    ],
)
def test_invalid_payload_never_reaches_provider(harness, body, status):
    with harness() as (client, requests, _):
        response = client.post("/v1/responses", headers=AUTH, content=body)
        assert response.status_code == status
        assert requests == []


def test_body_size_limit(harness):
    with harness(max_body_bytes=1024) as (client, requests, _):
        response = client.post(
            "/v1/responses",
            headers=AUTH,
            json={
                "model": "smart-auto",
                "input": "x" * 2048,
            },
        )
        assert response.status_code == 413
        assert not requests


def test_observer_handles_multiline_data_and_oversized_events():
    observed = []
    observer = SSEObserver(observed.append, max_event_bytes=128)
    observer.feed(b"data: " + b"x" * 256)
    observer.feed(b'\n\ndata: {\ndata: "response": {"id": "ok"}}\n\n')
    assert observed == [{"id": "ok"}]
