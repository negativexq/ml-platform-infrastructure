"""Separate process liveness from required-dependency readiness."""

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def add_readiness(app: FastAPI, check: Callable[[], None] | None = None) -> None:
    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            if check is not None:
                check()
        except Exception:  # noqa: BLE001 - dependency failure must remove this pod from traffic
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ok"})
