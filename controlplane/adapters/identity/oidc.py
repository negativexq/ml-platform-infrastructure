"""OpenID Connect: verifying bearer tokens and running the browser sign-in.

Works with any OIDC provider (Keycloak, Dex, Okta, Entra ID, Google, GitHub via Dex): the
issuer's discovery document says where the keys and endpoints are. Signing keys are fetched
from the provider's JWKS and cached; an unknown key id triggers one refetch, so key rotation
needs no restart.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection, Mapping
from functools import cached_property
from typing import Any

import jwt

from controlplane.domain.access import Principal
from controlplane.domain.errors import Unauthenticated

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
HTTP_TIMEOUT_SECONDS = 10
LEEWAY_SECONDS = 30


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
        data: dict[str, Any] = json.load(response)
    return data


class OidcProvider:
    def __init__(
        self,
        issuer: str,
        *,
        audience: str,
        client_id: str = "",
        client_secret: str = "",
        username_claim: str = "preferred_username",
        groups_claim: str = "groups",
        platform_admins: Collection[str] = (),
        scopes: str = "openid profile email",
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.client_id = client_id
        self._client_secret = client_secret
        self._username_claim = username_claim
        self._groups_claim = groups_claim
        self._platform_admins = frozenset(platform_admins)
        self._scopes = scopes

    # -- discovery and keys -------------------------------------------------------------

    @cached_property
    def metadata(self) -> dict[str, Any]:
        try:
            meta = _get_json(f"{self.issuer}/.well-known/openid-configuration")
        except (urllib.error.URLError, ValueError) as exc:
            raise Unauthenticated(f"identity provider unavailable: {exc}") from exc
        if meta.get("issuer", "").rstrip("/") != self.issuer:
            raise Unauthenticated(f"discovery document is for issuer {meta.get('issuer')!r}")
        return meta

    @cached_property
    def _keys(self) -> jwt.PyJWKClient:
        return jwt.PyJWKClient(self.metadata["jwks_uri"], cache_keys=True, lifespan=3600)

    def _decode(self, token: str, audience: str, nonce: str | None = None) -> dict[str, Any]:
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=audience,
                issuer=self.metadata["issuer"],
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.PyJWKClientError as exc:
            raise Unauthenticated(f"cannot verify the token's signature: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise Unauthenticated(f"invalid token: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise Unauthenticated("the sign-in response does not belong to this sign-in")
        return claims

    def principal(self, claims: Mapping[str, Any]) -> Principal:
        username = str(
            claims.get(self._username_claim) or claims.get("email") or claims.get("sub") or ""
        )
        if not username:
            raise Unauthenticated("the token names no user")
        raw = claims.get(self._groups_claim) or []
        groups = tuple(sorted({str(g).lstrip("/") for g in raw if str(g).strip("/")}))
        subjects = {f"user:{username}", *(f"group:{g}" for g in groups)}
        return Principal(
            username=username,
            groups=groups,
            email=claims.get("email"),
            display_name=claims.get("name"),
            platform_admin=bool(self._platform_admins & subjects),
        )

    # -- Authenticator: API calls with `Authorization: Bearer <access token>` -------------

    def authenticate(self, token: str) -> Principal:
        return self.principal(self._decode(token, self.audience))

    # -- LoginProvider: browser sign-in, server side ---------------------------------------

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.metadata['authorization_endpoint']}?{query}"

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> tuple[Principal, str | None]:
        body = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "client_id": self.client_id,
                **({"client_secret": self._client_secret} if self._client_secret else {}),
            }
        ).encode()
        request = urllib.request.Request(
            self.metadata["token_endpoint"],
            data=body,
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                tokens: dict[str, Any] = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise Unauthenticated(f"the identity provider refused the sign-in: {detail}") from exc
        except urllib.error.URLError as exc:
            raise Unauthenticated(f"identity provider unavailable: {exc}") from exc
        id_token = tokens.get("id_token")
        if not id_token:
            raise Unauthenticated("the identity provider returned no ID token")
        claims = self._decode(id_token, self.client_id, nonce=nonce)
        if claims.get("iat", 0) > time.time() + LEEWAY_SECONDS:
            raise Unauthenticated("the ID token was issued in the future")
        return self.principal(claims), id_token

    def logout_url(
        self, *, post_logout_redirect_uri: str, id_token_hint: str | None = None
    ) -> str | None:
        """Client-based logout; the provider may ask the user to confirm sign-out."""
        endpoint = self.metadata.get("end_session_endpoint")
        if not endpoint:
            return None
        params = {"client_id": self.client_id, "post_logout_redirect_uri": post_logout_redirect_uri}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        query = urllib.parse.urlencode(params)
        return f"{endpoint}?{query}"
