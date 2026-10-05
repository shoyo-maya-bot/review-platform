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
    def __init__(self, key):
        self.key = key


class _Jwks:
    def __init__(self, pub):
        self.pub = pub

    def get_signing_key_from_jwt(self, token):
        return _Key(self.pub)


@pytest.fixture(scope="module")
def keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k, k.public_key()


def _token(priv, **claims):
    base = {"iss": ISS, "sub": "user-1", "iat": int(time.time()), "exp": int(time.time()) + 300}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, priv, algorithm="RS256")


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
