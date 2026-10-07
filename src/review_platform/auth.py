"""API と MCP の認証（ASGI ミドルウェア）. 認証した利用者を scope["state"]["principal"] に入れる.

- token: Authorization: Bearer <token>. サーバーはトークンの SHA-256 だけを持つ
- jwt  : Cognito / Entra ID の JWT（RS256）. aud が無い Cognito のアクセストークンは client_id で確かめる.
         JWKS の取得は同期の通信なので、検証はスレッドプールで行う（イベントループを止めない）.
         知らない kid のトークンが来ても、JWKS の取り直しは JWKS_MIN_REFRESH_SECONDS に 1 回まで（取り直しの連打で窓口を叩かない）
- none : ローカルの試験だけ（config.py が 127.0.0.1 以外では起動させない）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict, deque

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

PUBLIC_PATHS = ("/healthz",)


class AuthError(Exception):
    pass


class TokenVerifier:
    def __init__(self, hashes: tuple[str, ...]):
        self.hashes = hashes

    def verify(self, token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for h in self.hashes:
            if hmac.compare_digest(digest, h):
                return f"token:{digest[:12]}"
        raise AuthError("invalid token")


JWKS_MIN_REFRESH_SECONDS = 60.0


class JwtVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        jwks_client: jwt.PyJWKClient | None = None,
        min_refresh_seconds: float = JWKS_MIN_REFRESH_SECONDS,
        clock=time.monotonic,
    ):
        self.issuer = issuer
        self.audience = audience
        self.jwks = jwks_client or jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)
        self.min_refresh_seconds = min_refresh_seconds
        self._clock = clock
        self._last_refresh: float | None = None
        self._lock = threading.Lock()

    def _signing_key(self, token: str):
        """kid に合う鍵. 手元の一覧に無ければ取り直すが、取り直しは min_refresh_seconds に 1 回まで."""
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            raise AuthError("invalid jwt: no kid")
        for k in self.jwks.get_signing_keys():
            if k.key_id == kid:
                return k.key
        with self._lock:
            now = self._clock()
            if self._last_refresh is not None and now - self._last_refresh < self.min_refresh_seconds:
                raise AuthError("invalid jwt: unknown kid")
            self._last_refresh = now
        for k in self.jwks.get_signing_keys(refresh=True):
            if k.key_id == kid:
                return k.key
        raise AuthError("invalid jwt: unknown kid")

    def verify(self, token: str) -> str:
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"], "verify_aud": False},
                leeway=30,
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid jwt: {type(e).__name__}") from e
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else list(aud or [])
        if auds:
            if self.audience not in auds:
                raise AuthError("audience mismatch")
        elif not (claims.get("token_use") == "access" and claims.get("client_id") == self.audience):
            raise AuthError("audience mismatch")
        return f"jwt:{claims['sub']}"


def build_verifier(s: Settings):
    if s.auth == "token":
        return TokenVerifier(s.api_token_hashes)
    if s.auth == "jwt":
        return JwtVerifier(s.jwt_issuer, s.jwt_audience, s.jwt_jwks_url)
    return None


async def _json(send: Send, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body, ensure_ascii=False).encode()
    h = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()), (b"cache-control", b"no-store")]
    await send({"type": "http.response.start", "status": status, "headers": h + (headers or [])})
    await send({"type": "http.response.body", "body": data})


class AuthMiddleware:
    def __init__(self, app: ASGIApp, verifier, rate_limit_per_minute: int = 30):
        self.app = app
        self.verifier = verifier
        self.limit = rate_limit_per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def _rate_limited(self, principal: str) -> bool:
        now = time.monotonic()
        q = self.hits[principal]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            return True
        q.append(now)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        if self.verifier is None:
            principal = "local"
        else:
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                await _json(send, 401, {"error": "unauthorized", "message": "Authorization: Bearer <token> が必要です"}, [(b"www-authenticate", b"Bearer")])
                return
            try:
                # JWT の検証は JWKS の同期の通信（最大 10 秒）を含みうるので、スレッドプールで行う
                principal = await run_in_threadpool(self.verifier.verify, token.strip())
            except AuthError:
                await _json(send, 401, {"error": "unauthorized", "message": "トークンが無効です"}, [(b"www-authenticate", b'Bearer error="invalid_token"')])
                return
        if scope["method"] == "POST" and self._rate_limited(principal):
            await _json(send, 429, {"error": "rate_limited", "message": "しばらく待ってからやり直してください"}, [(b"retry-after", b"60")])
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def principal_of(scope_or_state) -> str:
    state = scope_or_state.get("state", {}) if isinstance(scope_or_state, dict) else scope_or_state
    p = state.get("principal") if isinstance(state, dict) else getattr(state, "principal", None)
    if not p:
        raise AuthError("not authenticated")
    return str(p)
