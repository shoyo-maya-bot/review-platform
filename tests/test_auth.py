from __future__ import annotations

import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from review_platform.auth import AuthError, JwtVerifier, TokenVerifier

ISS = "https://cognito-idp.ap-northeast-1.amazonaws.com/ap-northeast-1_example"
AUD = "client-abc"


class _Key:
    def __init__(self, key, kid="k1"):
        self.key, self.key_id = key, kid


class _Jwks:
    """PyJWKClient の代わり. get_signing_keys(refresh=True) の回数を数える."""

    def __init__(self, pub, kids=("k1",)):
        self.pub, self.kids, self.refreshes = pub, list(kids), 0
        self.cached = list(self.kids)  # 窓口から最後に取った一覧（refresh=True で取り直す）

    def get_signing_keys(self, refresh=False):
        if refresh:
            self.refreshes += 1
            self.cached = list(self.kids)
        return [_Key(self.pub, k) for k in self.cached]


@pytest.fixture(scope="module")
def keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k, k.public_key()


def _token(priv, kid="k1", **claims):
    base = {"iss": ISS, "sub": "user-1", "iat": int(time.time()), "exp": int(time.time()) + 300}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, priv, algorithm="RS256", headers={"kid": kid})


def test_token_verifier():
    v = TokenVerifier((hashlib.sha256(b"secret-1").hexdigest(),))
    assert v.verify("secret-1").startswith("token:")
    with pytest.raises(AuthError):
        v.verify("secret-2")


def test_jwt_with_aud_and_cognito_access_token(keypair):
    priv, pub = keypair
    v = JwtVerifier(ISS, AUD, "unused", jwks_client=_Jwks(pub))
    assert v.verify(_token(priv, aud=AUD)) == "jwt:user-1"  # Entra ID・Cognito の ID トークン
    assert v.verify(_token(priv, token_use="access", client_id=AUD)) == "jwt:user-1"  # Cognito のアクセストークン（aud なし）


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "other"},
        {"token_use": "access", "client_id": "other"},
        {"token_use": "id"},  # aud も client_id も無い
        {"aud": AUD, "iss": "https://evil.example.com"},
        {"aud": AUD, "exp": int(time.time()) - 3600},
        {"aud": AUD, "sub": None},
    ],
)
def test_jwt_rejects(keypair, claims):
    priv, pub = keypair
    v = JwtVerifier(ISS, AUD, "unused", jwks_client=_Jwks(pub))
    with pytest.raises(AuthError):
        v.verify(_token(priv, **claims))


def test_jwt_rejects_hs256_confusion(keypair):
    _, pub = keypair
    v = JwtVerifier(ISS, AUD, "unused", jwks_client=_Jwks(pub))
    forged = jwt.encode({"iss": ISS, "sub": "x", "aud": AUD, "iat": int(time.time()), "exp": int(time.time()) + 60}, "shared-secret-shared-secret-shared-secret", algorithm="HS256")
    with pytest.raises(AuthError):
        v.verify(forged)


def test_unknown_kid_refreshes_jwks_at_most_once_per_interval(keypair):
    priv, pub = keypair
    now = [1000.0]
    jwks = _Jwks(pub)
    v = JwtVerifier(ISS, AUD, "unused", jwks_client=jwks, min_refresh_seconds=60, clock=lambda: now[0])
    for _ in range(20):  # 知らない kid の要求を連打しても
        with pytest.raises(AuthError):
            v.verify(_token(priv, kid="unknown", aud=AUD))
    assert jwks.refreshes == 1  # 取り直しは 1 回だけ
    now[0] += 61
    jwks.kids.append("rotated")  # 鍵の入れ替え後は、間隔が空けば取り直して受け付ける
    assert v.verify(_token(priv, kid="rotated", aud=AUD)) == "jwt:user-1"
    assert jwks.refreshes == 2
    assert v.verify(_token(priv, aud=AUD)) == "jwt:user-1" and jwks.refreshes == 2  # 知っている kid は取り直さない


def test_token_without_kid_is_rejected(keypair):
    priv, pub = keypair
    v = JwtVerifier(ISS, AUD, "unused", jwks_client=_Jwks(pub))
    token = jwt.encode({"iss": ISS, "sub": "u", "aud": AUD, "iat": int(time.time()), "exp": int(time.time()) + 60}, priv, algorithm="RS256")
    with pytest.raises(AuthError):
        v.verify(token)


def test_middleware_verifies_in_threadpool_without_blocking_event_loop():
    """検証が遅く（JWKS の取得で止まる）ても、ほかの要求（/healthz など）はイベントループで進む."""
    import asyncio
    import threading

    from review_platform.auth import AuthMiddleware

    gate = threading.Event()
    seen_threads = []

    class SlowVerifier:
        def verify(self, token):
            seen_threads.append(threading.current_thread() is threading.main_thread())
            gate.wait(5)
            return "jwt:u"

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = AuthMiddleware(app, SlowVerifier())

    async def call(path, headers):
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request", "body": b""}

        await mw({"type": "http", "path": path, "method": "GET", "headers": headers}, receive, send)
        return sent[0]["status"]

    async def main():
        slow = asyncio.create_task(call("/v1/jobs", [(b"authorization", b"Bearer x")]))
        await asyncio.sleep(0.05)
        fast = await asyncio.wait_for(call("/healthz", []), timeout=1)  # 止まっていなければすぐ返る
        gate.set()
        return fast, await slow

    assert asyncio.run(main()) == (200, 200)
    assert seen_threads == [False]  # 検証はメインスレッド（イベントループ）の外で動いた
