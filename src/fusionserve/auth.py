"""JWT verification module using PyJWKClient with built-in JWKS caching."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from litestar.exceptions import NotAuthorizedException
from pydantic import BaseModel

from .config import settings

_logger = logging.getLogger(settings.app_name)

# Module-level PyJWKClient — lazily initialized on first use.
# Its built-in two-tier caching (JWK Set with 5-min TTL + per-kid LRU)
# avoids redundant JWKS network fetches across requests.
jwk_client: PyJWKClient | None = None


async def _resolve_jwks_url() -> str:
    """Return the effective JWKS URL, resolving via OIDC well-known if needed."""
    # Direct config takes precedence
    if settings.jwks_url:
        return settings.jwks_url

    # Discover via OIDC well-known, fail otherwise
    assert settings.jwt_issuer is not None
    well_known_url = f"{settings.jwt_issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
        resp = await client.get(well_known_url, timeout=10)
        resp.raise_for_status()

    return resp.json()["jwks_uri"]


async def _get_jwk_client() -> PyJWKClient:
    """Return the module-level PyJWKClient, initializing it on first call."""
    global jwk_client  # noqa: PLW0603
    if jwk_client is None:
        jwks_url = await _resolve_jwks_url()
        jwk_client = PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=300,
            cache_keys=True,
            max_cached_keys=16,
        )
    return jwk_client


async def verify_and_decode(token: str) -> dict[str, Any]:
    """Verify a JWT using JWKS and return the verified claims.

    Raises ``NotAuthorizedException`` on any authentication failure
    (malformed token, expired, bad signature, untrusted issuer, network
    errors fetching JWKS, etc.).
    """
    try:
        # 1. Decode without verification to extract issuer
        unverified_claims = jwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
        issuer = unverified_claims.get("iss")
        if not issuer:
            raise NotAuthorizedException("Token missing 'iss' claim")

        # 2. Validate issuer against configured jwt_issuer (if set)
        if settings.jwt_issuer and issuer != settings.jwt_issuer:
            raise NotAuthorizedException("Untrusted issuer")

        # 3. Get the module-level cached PyJWKClient
        client = await _get_jwk_client()

        # 4. Get the signing key (synchronous — offload to thread).
        #    PyJWKClient handles kid extraction, caching, and
        #    automatic refresh on kid miss.
        signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)

        # 5. Full token verification (signature + exp + nbf + iss)
        return jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            issuer=settings.jwt_issuer if settings.jwt_issuer else issuer,
        )

    except NotAuthorizedException:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise NotAuthorizedException("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        _logger.warning("JWT verification failed: %s", exc)
        raise NotAuthorizedException("Invalid token") from exc
    except Exception as exc:
        _logger.warning("JWT verification failed: %s", exc)
        raise NotAuthorizedException("Invalid token") from exc


class User(BaseModel):
    id: str
    name: str


class Token(BaseModel):
    token: str


async def retrieve_user_handler(token: str) -> User | None:
    # TODO: complete the logic handling roles
    """Verify the JWT and return a User from the claims, or None on failure."""
    try:
        claims = await verify_and_decode(token)
    except Exception:
        return None

    sub = claims.get("sub")
    if not sub:
        return None

    name = claims.get("name") or claims.get("preferred_username") or sub
    return User(id=sub, name=name)
