"""Unit tests for ``fusionserve.auth.verify_and_decode``.

The function fans out to PyJWKClient (synchronous, runs in a thread) and
``jwt.decode`` for verification. The tests below mock both seams and assert
the public contract:

- malformed / unsigned / expired / wrong-issuer tokens raise
  ``NotAuthorizedException``;
- a well-formed token whose signature verifies returns the decoded claims.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from litestar.exceptions import NotAuthorizedException

from fusionserve import auth as auth_module
from fusionserve.auth import verify_and_decode

ISSUER = "https://issuer.example.com"


@pytest.fixture
def rsa_keypair():
    """Generate a fresh RSA keypair so we can sign valid test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def reset_jwk_client(monkeypatch):
    """Force a fresh PyJWKClient resolution for every test."""
    monkeypatch.setattr(auth_module, "jwk_client", None)


@pytest.fixture(autouse=True)
def configure_settings(monkeypatch):
    """Pin issuer-related config so tests are independent of ``.env``."""
    monkeypatch.setattr(auth_module.settings, "jwt_issuer", ISSUER)
    monkeypatch.setattr(auth_module.settings, "jwks_url", f"{ISSUER}/jwks")


@pytest.fixture
def stub_jwk_client(monkeypatch, rsa_keypair):
    """Replace ``_get_jwk_client`` with a stub returning a fixed signing key.

    Production code passes the value of ``client.get_signing_key_from_jwt``
    straight to ``jwt.decode``. PyJWT accepts cryptography key objects, so
    the stub returns the public key directly to match how PyJWKClient
    eventually unwraps a JWK.
    """
    private_key = rsa_keypair

    class _StubClient:
        def get_signing_key_from_jwt(self, _token):
            return private_key.public_key()

    async def _get():
        return _StubClient()

    monkeypatch.setattr(auth_module, "_get_jwk_client", _get)
    return private_key


async def test_returns_claims_for_valid_token(stub_jwk_client):
    private_key = stub_jwk_client
    now = int(time.time())
    payload = {
        "sub": "user-1",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    result = await verify_and_decode(token)
    assert result["sub"] == "user-1"
    assert result["iss"] == ISSUER


async def test_rejects_token_without_iss_claim(stub_jwk_client):
    private_key = stub_jwk_client
    now = int(time.time())
    token = jwt.encode({"sub": "u", "iat": now, "exp": now + 60}, private_key, algorithm="RS256")
    with pytest.raises(NotAuthorizedException):
        await verify_and_decode(token)


async def test_rejects_untrusted_issuer(stub_jwk_client):
    private_key = stub_jwk_client
    now = int(time.time())
    payload = {
        "sub": "u",
        "iss": "https://attacker.example.com",
        "iat": now,
        "exp": now + 60,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    with pytest.raises(NotAuthorizedException):
        await verify_and_decode(token)


async def test_rejects_expired_token(stub_jwk_client):
    private_key = stub_jwk_client
    now = int(time.time())
    payload = {
        "sub": "u",
        "iss": ISSUER,
        "iat": now - 120,
        "exp": now - 60,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    with pytest.raises(NotAuthorizedException):
        await verify_and_decode(token)


async def test_rejects_garbage_token(stub_jwk_client):
    with pytest.raises(NotAuthorizedException):
        await verify_and_decode("not-a-jwt")


async def test_rejects_token_with_bad_signature(monkeypatch, rsa_keypair):
    """A token signed with a *different* key must not verify."""
    private_key = rsa_keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _StubClient:
        def get_signing_key_from_jwt(self, _token):
            # The "wrong" public key — signature verification must fail.
            return other_key.public_key()

    async def _get():
        return _StubClient()

    monkeypatch.setattr(auth_module, "_get_jwk_client", _get)

    now = int(time.time())
    payload = {"sub": "u", "iss": ISSUER, "iat": now, "exp": now + 60}
    token = jwt.encode(payload, private_key, algorithm="RS256")
    with pytest.raises(NotAuthorizedException):
        await verify_and_decode(token)


async def test_resolve_jwks_url_raises_runtimeerror_when_unconfigured(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "jwks_url", None)
    monkeypatch.setattr(auth_module.settings, "jwt_issuer", None)
    with pytest.raises(RuntimeError):
        await auth_module._resolve_jwks_url()
