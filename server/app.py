import copy
import hmac
import io
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
import zstandard
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from config.settings import Settings
from providers.upstream import SSEObserver, chatgpt_headers, response_headers, upstream_headers
from router.project_analyzer import ProjectAccessError
from router.types import RouteDecision, RoutePreview
from server.service import MissingRouteError, RouterService

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=32000)
    project_root: str | None = None


async def read_payload(
    request: Request, max_bytes: int, *, allow_explicit_models: bool = False
) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > max_bytes:
            raise APIError(413, "request_too_large", "Request body exceeds configured limit")
    encoding = request.headers.get("content-encoding", "identity").casefold().strip()
    if encoding == "zstd":
        try:
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
                data = reader.read(max_bytes + 1)
        except zstandard.ZstdError as exc:
            raise APIError(400, "invalid_compressed_body", "Invalid zstd request body") from exc
        if len(data) > max_bytes:
            raise APIError(
                413, "request_too_large", "Decoded request body exceeds configured limit"
            )
    elif encoding != "identity":
        raise APIError(415, "unsupported_encoding", "Supported request encodings: identity, zstd")
    try:

        def reject_constant(_: str) -> None:
            raise ValueError("Non-finite JSON number")

        value = json.loads(data, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise APIError(400, "invalid_json", "Request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise APIError(400, "invalid_request", "Request body must be a JSON object")
    model = value.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise APIError(400, "invalid_model", "model must be a model identifier")
    if model != "smart-auto" and not allow_explicit_models:
        raise APIError(404, "model_not_found", "This provider exposes model smart-auto")
    if not isinstance(value.get("input"), (str, list)):
        raise APIError(400, "invalid_input", "input must be a string or a Responses input array")
    if "stream" in value and not isinstance(value["stream"], bool):
        raise APIError(400, "invalid_stream", "stream must be a boolean")
    for key in ("previous_response_id", "prompt_cache_key"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise APIError(400, "invalid_request", f"{key} must be a string")
    if value.get("reasoning") is not None and not isinstance(value["reasoning"], dict):
        raise APIError(400, "invalid_reasoning", "reasoning must be an object")
    effort = (value.get("reasoning") or {}).get("effort")
    if effort is not None and (
        not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", effort)
    ):
        raise APIError(400, "invalid_reasoning", "reasoning.effort must be an effort identifier")
    return value


def route_headers(decision: RouteDecision) -> dict[str, str]:
    return {
        "x-smart-router-model": decision.model,
        "x-smart-router-reasoning": decision.reasoning,
        "x-smart-router-source": decision.source,
    }


def create_app(
    settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        upstream = client or httpx.AsyncClient(
            base_url=settings.inference_base_url + "/",
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=10),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            app.state.router = RouterService(settings, upstream)
            yield
        finally:
            if client is None:
                await upstream.aclose()

    app = FastAPI(title="Codex Smart Router", version="0.1.0", lifespan=lifespan)

    async def authenticate_local(request: Request) -> None:
        key = settings.api_key.get_secret_value() if settings.api_key else ""
        if not key:
            raise APIError(503, "router_not_configured", "Set SMART_ROUTER_API_KEY before use")
        provided = request.headers.get("authorization", "")
        scheme, _, token = provided.partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(token.encode(), key.encode()):
            raise APIError(401, "invalid_api_key", "A valid router bearer token is required")

    async def authenticate(request: Request) -> None:
        if settings.auth_mode == "api":
            return await authenticate_local(request)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise APIError(
                401, "chatgpt_sign_in_required", "Codex must send its signed-in account token"
            )
        # The official backend validates and authorizes the account token. Do not copy it
        # to disk, log it, refresh it here, or replace it with a local/API credential.

    @app.exception_handler(APIError)
    async def api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "type": "router_error",
                    "code": exc.code,
                    "message": exc.message,
                }
            },
        )

    @app.exception_handler(ProjectAccessError)
    async def project_error(_: Request, exc: ProjectAccessError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "router_error",
                    "code": "project_access_denied",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(MissingRouteError)
    async def missing_route(_: Request, exc: MissingRouteError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "router_error",
                    "code": "routing_state_missing",
                    "message": str(exc),
                }
            },
        )

    @app.get("/healthz")
    async def health() -> dict:
        return {
            "status": "ok",
            "auth_mode": settings.auth_mode,
            "router_configured": bool(settings.api_key and settings.api_key.get_secret_value()),
            "upstream_configured": bool(
                settings.auth_mode == "chatgpt"
                or (settings.upstream_api_key and settings.upstream_api_key.get_secret_value())
            ),
            "classifier_enabled": settings.classifier_enabled,
        }

    @app.get("/v1/models", dependencies=[Depends(authenticate)])
    async def models() -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": "smart-auto",
                    "object": "model",
                    "created": 0,
                    "owned_by": "smart-router",
                }
            ],
        }

    @app.post("/v1/route", response_model=RoutePreview, dependencies=[Depends(authenticate_local)])
    async def preview(payload: PreviewRequest, request: Request) -> RoutePreview:
        return await request.app.state.router.preview(payload.task, payload.project_root)

    async def proxy(request: Request, endpoint: str) -> Response:
        body = await read_payload(
            request,
            settings.max_body_bytes,
            allow_explicit_models=settings.auth_mode == "chatgpt",
        )
        key = settings.upstream_api_key.get_secret_value() if settings.upstream_api_key else ""
        if settings.auth_mode == "api" and not key:
            raise APIError(503, "upstream_not_configured", "Set SMART_ROUTER_UPSTREAM_API_KEY")
        service: RouterService = request.app.state.router
        decision = await service.resolve(body, dict(request.headers))
        payload = copy.deepcopy(body)
        payload["model"] = decision.model
        if endpoint == "responses" and body["model"] == "smart-auto":
            payload["reasoning"] = {
                **(payload.get("reasoning") or {}),
                "effort": decision.reasoning,
            }
        if isinstance(payload.get("metadata"), dict):
            payload["metadata"].pop("smart_router_project_root", None)
        streaming = endpoint == "responses" and body.get("stream", False)
        headers = (
            chatgpt_headers(dict(request.headers))
            if settings.auth_mode == "chatgpt"
            else upstream_headers(key, dict(request.headers))
        )
        upstream = None
        try:
            upstream_request = service.client.build_request(
                "POST", endpoint, json=payload, headers=headers
            )
            upstream = await service.client.send(upstream_request, stream=True)
            outgoing = {**response_headers(upstream), **route_headers(decision)}
            logger.info(
                "route model=%s reasoning=%s source=%s upstream_status=%s",
                decision.model,
                decision.reasoning,
                decision.source,
                upstream.status_code,
            )
            if not streaming or upstream.is_error:
                data = await upstream.aread()
                if not upstream.is_error and endpoint == "responses":
                    try:
                        value = json.loads(data)
                        if isinstance(value, dict):
                            service.cache.remember_response(value, decision)
                    except ValueError:
                        pass
                await upstream.aclose()
                return Response(data, status_code=upstream.status_code, headers=outgoing)
            content_type = upstream.headers.get("content-type", "")
            if not content_type and settings.auth_mode == "chatgpt":
                # Some native Codex responses omit MIME metadata. The client consumes
                # the SSE framing directly; retain the bytes and supply the SSE MIME type.
                outgoing["content-type"] = "text/event-stream"
            elif "text/event-stream" not in content_type:
                logger.warning(
                    "Unexpected account stream content type: %s",
                    upstream.headers.get("content-type"),
                )
                await upstream.aclose()
                raise APIError(502, "invalid_upstream_stream", "Expected a Responses SSE stream")
        except httpx.TimeoutException as exc:
            if upstream:
                await upstream.aclose()
            raise APIError(
                504, "upstream_timeout", "Upstream timed out; request was not replayed"
            ) from exc
        except httpx.HTTPError as exc:
            if upstream:
                await upstream.aclose()
            raise APIError(
                502, "upstream_unavailable", "Upstream unavailable; request was not replayed"
            ) from exc

        async def stream():
            observer = SSEObserver(lambda value: service.cache.remember_response(value, decision))
            try:
                async for chunk in upstream.aiter_bytes():
                    observer.feed(chunk)
                    yield chunk
            except httpx.HTTPError:
                logger.warning("Upstream stream interrupted; no automatic replay")
                error = {
                    "type": "error",
                    "code": "upstream_stream_error",
                    "message": "Upstream stream interrupted; request was not replayed",
                }
                yield ("event: error\ndata: " + json.dumps(error) + "\n\n").encode()
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            headers={**outgoing, "cache-control": "no-cache"},
        )

    @app.post("/v1/responses", dependencies=[Depends(authenticate)])
    async def responses(request: Request) -> Response:
        return await proxy(request, "responses")

    @app.post("/v1/responses/compact", dependencies=[Depends(authenticate)])
    async def compact(request: Request) -> Response:
        return await proxy(request, "responses/compact")

    # Codex's ChatGPT gateway contract keeps the native backend path, not /v1.
    app.add_api_route(
        "/backend-api/codex/responses",
        responses,
        methods=["POST"],
        dependencies=[Depends(authenticate)],
    )
    app.add_api_route(
        "/backend-api/codex/responses/compact",
        compact,
        methods=["POST"],
        dependencies=[Depends(authenticate)],
    )

    @app.get("/backend-api/codex/models", dependencies=[Depends(authenticate)])
    async def account_models(request: Request) -> Response:
        if settings.auth_mode != "chatgpt":
            raise APIError(404, "account_mode_required", "Native models require ChatGPT mode")
        try:
            content, status, headers = await request.app.state.router.account_catalog.get(
                dict(request.headers),
                request.query_params,
            )
        except httpx.TimeoutException as exc:
            logger.warning("Account model discovery timed out: %s", type(exc).__name__)
            raise APIError(504, "upstream_timeout", "Account model discovery timed out") from exc
        except httpx.HTTPError as exc:
            logger.warning("Account model discovery failed: %s", type(exc).__name__)
            raise APIError(
                502, "upstream_unavailable", "Account model discovery unavailable"
            ) from exc
        if 200 <= status < 300:
            try:
                catalog = json.loads(content)
                if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
                    raise ValueError("Expected native Codex model catalog")
                auto = json.loads(settings.catalog_file.read_text())["models"][0]
                native_models = [
                    model
                    for model in catalog["models"]
                    if isinstance(model, dict) and model.get("slug") != "smart-auto"
                ]
                template = next(
                    (model for model in native_models if model.get("slug") == "gpt-5.6-sol"),
                    None,
                )
                if template and template.get("base_instructions"):
                    auto["base_instructions"] = template["base_instructions"]
                # Forward full Responses over HTTP/SSE, preserving manual choices.
                catalog["models"] = [auto, *native_models]
                for model in catalog["models"]:
                    model["use_responses_lite"] = False
                return JSONResponse(catalog, headers=headers)
            except (ValueError, KeyError, OSError) as exc:
                raise APIError(502, "invalid_model_catalog", "Invalid Codex model catalog") from exc
        return Response(content, status_code=status, headers=headers)

    return app


app = create_app()
