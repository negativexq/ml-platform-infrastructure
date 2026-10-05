"""The gateway's HTTP surface. Public, so it says as little as it can: one error shape, the
same 404 for missing and internal endpoints, and no internals in messages.

  POST /v1/{project}/{endpoint}/predict             models (v2-infer)
  POST /v1/{project}/{endpoint}/chat/completions    LLMs (openai), reserved
  POST /v1/{project}/{endpoint}/invoke              functions (http), reserved
  GET  /healthz                                     liveness
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from controlplane.application.gateway import GatewayError, GatewayService
from controlplane.health import add_readiness

MAX_BODY_BYTES = 10 * 1024 * 1024  # before any endpoint's own (smaller) limit applies
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_PREDICT_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "description": 'For a model (v2-infer): {"instances": [[...], ...]}',
                },
                "example": {"instances": [[0.42, 1200, 3, 0.18]]},
            }
        },
    }
}


def _request_id(request: Request) -> str:
    given = request.headers.get("x-request-id", "")
    return given if _REQUEST_ID.match(given) else f"req_{uuid.uuid4().hex[:16]}"


def _error(error: GatewayError, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": error.message, "request_id": request_id}},
        status_code=error.status,
        headers={**error.headers, "X-Request-Id": request_id},
    )


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def _read(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise GatewayError(413, "too_large", "the body is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise GatewayError(413, "too_large", "the body is too large")
    return bytes(body)


def create_gateway(
    service: GatewayService, *, readiness: Callable[[], None] | None = None
) -> FastAPI:
    app = FastAPI(
        title="ML Platform Gateway",
        version="1",
        description="Call a project's public endpoints with an API key "
        "(`Authorization: Bearer mlp_live_...`).",
    )
    add_readiness(app, readiness)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/{project}/{endpoint}/{operation:path}",
        summary="Call an endpoint (predict for models)",
        openapi_extra=_PREDICT_BODY,
        response_class=Response,
    )
    async def call(project: str, endpoint: str, operation: str, request: Request) -> Response:
        request_id = _request_id(request)
        try:
            body = await _read(request)
            reply = await service.invoke(
                token=_bearer(request),
                project=project,
                endpoint=endpoint,
                operation=operation,
                body=body,
                request_id=request_id,
            )
        except GatewayError as error:
            return _error(error, request_id)
        return StreamingResponse(
            reply.chunks,
            status_code=reply.status,
            media_type=reply.content_type,
            headers={**reply.headers, "X-Request-Id": request_id},
        )

    @app.exception_handler(404)
    async def not_found(request: Request, _exc: Exception) -> JSONResponse:
        error = GatewayError(404, "not_found", "use POST /v1/{project}/{endpoint}/predict")
        return _error(error, _request_id(request))

    @app.exception_handler(405)
    async def wrong_method(request: Request, _exc: Exception) -> JSONResponse:
        return _error(GatewayError(405, "method_not_allowed", "use POST"), _request_id(request))

    return app
