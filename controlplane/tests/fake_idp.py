"""A small but real OpenID Connect provider for tests: discovery, JWKS, authorization code
with PKCE, RS256-signed tokens. Signs in whoever `next_user` names, without a login page."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

CLIENT_ID, CLIENT_SECRET, API_AUDIENCE = "mlp-ui", "s3cret-for-tests", "mlp"


class FakeIdP:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key-1"
        self.next_user: dict[str, Any] = {"preferred_username": "alice", "groups": []}
        self._codes: dict[str, dict[str, Any]] = {}
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.issuer = f"http://127.0.0.1:{self.port}/realms/test"
        self.app = self._build()
        self._server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> FakeIdP:
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake IdP did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    def token(
        self,
        username: str = "alice",
        groups: list[str] | None = None,
        *,
        aud: str = API_AUDIENCE,
        ttl: int = 300,
        key: Any = None,
        issuer: str | None = None,
        **claims: Any,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": issuer or self.issuer,
            "sub": f"sub-{username}",
            "aud": aud,
            "iat": now,
            "exp": now + ttl,
            "preferred_username": username,
            "groups": groups or [],
            **claims,
        }
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def _build(self) -> FastAPI:
        app = FastAPI()
        idp = self
        base = "/realms/test"

        @app.get(f"{base}/.well-known/openid-configuration")
        def discovery() -> dict[str, Any]:
            return {
                "issuer": idp.issuer,
                "authorization_endpoint": f"{idp.issuer}/auth",
                "token_endpoint": f"{idp.issuer}/token",
                "jwks_uri": f"{idp.issuer}/certs",
                "end_session_endpoint": f"{idp.issuer}/logout",
            }

        @app.get(f"{base}/certs")
        def certs() -> dict[str, Any]:
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(idp.key.public_key(), as_dict=True)
            return {"keys": [{**jwk, "kid": idp.kid, "use": "sig", "alg": "RS256"}]}

        @app.get(f"{base}/auth")
        def authorize(request: Request) -> RedirectResponse:
            q = request.query_params
            if q.get("client_id") != CLIENT_ID or q.get("code_challenge_method") != "S256":
                raise HTTPException(400, "bad authorization request")
            code = secrets.token_urlsafe(16)
            idp._codes[code] = {
                "user": dict(idp.next_user),
                "nonce": q["nonce"],
                "challenge": q["code_challenge"],
                "redirect_uri": q["redirect_uri"],
            }
            return RedirectResponse(
                f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", 302
            )

        @app.post(f"{base}/token")
        async def token(request: Request) -> JSONResponse:
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
            grant_type, code, redirect_uri = (
                form.get("grant_type"),
                form.get("code", ""),
                form.get("redirect_uri"),
            )
            code_verifier, client_id = form.get("code_verifier", ""), form.get("client_id")
            client_secret = form.get("client_secret", "")
            pending = idp._codes.pop(code, None)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                grant_type != "authorization_code"
                or pending is None
                or client_id != CLIENT_ID
                or client_secret != CLIENT_SECRET
                or redirect_uri != pending["redirect_uri"]
                or challenge != pending["challenge"]
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            user = pending["user"]
            id_token = idp.token(
                user["preferred_username"],
                user.get("groups"),
                aud=CLIENT_ID,
                nonce=pending["nonce"],
                name=user.get("name"),
                email=user.get("email"),
            )
            return JSONResponse(
                {
                    "id_token": id_token,
                    "access_token": idp.token(user["preferred_username"]),
                    "token_type": "Bearer",
                }
            )

        return app
