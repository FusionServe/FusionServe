"""JWT verification module using PyJWKClient with built-in JWKS caching."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
import jwt
from jsonpath import JSONPointer
from jwt import PyJWKClient
from litestar.exceptions import NotAuthorizedException
from pydantic import BaseModel, EmailStr

from .config import settings

_logger = logging.getLogger(settings.app_name)

# Module-level PyJWKClient — lazily initialized on first use.
# Its built-in two-tier caching (JWK Set with 5-min TTL + per-kid LRU)
# avoids redundant JWKS network fetches across requests.
jwk_client: PyJWKClient | None = None


async def _resolve_jwks_url() -> str:
    """Return the effective JWKS URL, resolving via OIDC well-known if needed.

    If ``settings.jwks_url`` is configured, it takes precedence and is
    returned directly. Otherwise, the URL is discovered by fetching the
    OIDC ``.well-known/openid-configuration`` document from
    ``settings.jwt_issuer`` and extracting the ``jwks_uri`` field.

    Returns:
        The URL of the JSON Web Key Set (JWKS) endpoint.

    Raises:
        RuntimeError: If neither ``settings.jwks_url`` nor
            ``settings.jwt_issuer`` is configured.
        httpx.HTTPStatusError: If the OIDC discovery request fails.
        KeyError: If the discovery document lacks a ``jwks_uri`` field.
    """
    # Direct config takes precedence
    if settings.jwks_url:
        return settings.jwks_url

    # Discover via OIDC well-known, fail otherwise
    if settings.jwt_issuer is None:
        raise RuntimeError("Either settings.jwks_url or settings.jwt_issuer must be configured to verify JWTs")
    well_known_url = f"{settings.jwt_issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
        resp = await client.get(well_known_url, timeout=10)
        resp.raise_for_status()

    return resp.json()["jwks_uri"]


async def _get_jwk_client() -> PyJWKClient:
    """Return the module-level PyJWKClient, initializing it on first call.

    On first invocation the JWKS URL is resolved and a ``PyJWKClient`` is
    created with caching enabled (5-minute JWK set TTL and a per-kid LRU
    of up to 16 keys). Subsequent calls return the same cached client.

    Returns:
        The shared ``PyJWKClient`` instance used for signing key lookups.
    """
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

    The token is decoded once without verification to extract the ``iss``
    claim, which is validated against ``settings.jwt_issuer`` when
    configured. The signing key is then fetched from the JWKS endpoint
    (via the cached ``PyJWKClient``) and used to fully verify the token,
    including signature, expiration, not-before, and issuer.

    Args:
        token: The encoded JWT string to verify.

    Returns:
        The decoded and verified JWT claims as a dictionary.

    Raises:
        NotAuthorizedException: On any authentication failure, including
            a malformed token, expired signature, invalid signature,
            untrusted issuer, missing ``iss`` claim, or network errors
            while fetching the JWKS.
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
        # TODO: evaluate an async native alternative
        signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)

        # 5. Full token verification (signature + exp + nbf + iss)
        return jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            issuer=settings.jwt_issuer if settings.jwt_issuer else issuer,
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise NotAuthorizedException("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        _logger.warning("JWT verification failed: %s", exc)
        raise NotAuthorizedException("Invalid token") from exc
    except (httpx.HTTPError, jwt.PyJWKClientError) as exc:
        # Network failure fetching JWKS, or PyJWKClient could not resolve a key.
        _logger.warning("JWKS resolution failed: %s", exc)
        raise NotAuthorizedException("Invalid token") from exc


class User(BaseModel):
    """Authenticated user derived from verified JWT claims.

    Attributes:
        id: Unique identifier for the user.
        username: Username used to identify the user in the application.
        email: Optional email address.
        display_name: Optional human-readable display name.
        first_name: Optional given name.
        surname: Optional family name.
        role: Optional single primary role.
        roles: Optional list of roles assigned to the user.
    """

    id: uuid.UUID
    username: str
    email: EmailStr | None = None
    display_name: str | None = None
    first_name: str | None = None
    surname: str | None = None
    role: str | None = None
    roles: list[str] | None = None


async def retrieve_user_handler(token: str) -> User | None:
    """Verify the JWT and return a User from the claims, or None on failure.

    The token is verified via ``verify_and_decode`` and the resulting
    claims are mapped to a ``User`` using the JSON pointers configured
    in ``settings.claims_map``. If the token lacks a ``sub`` claim,
    ``None`` is returned.

    Args:
        token: The encoded JWT string to verify and map.

    Returns:
        A populated ``User`` instance on success, or ``None`` if the
        token has no ``sub`` claim.

    Raises:
        NotAuthorizedException: If the token cannot be verified (see
            ``verify_and_decode``).
    """
    payload = await verify_and_decode(token)
    sub = payload.get("sub")
    if not sub:
        return None
    user = User(
        id=JSONPointer(settings.claims_map.id).resolve(payload),
        username=JSONPointer(settings.claims_map.username).resolve(payload),
        email=JSONPointer(settings.claims_map.email).resolve(payload),
        display_name=JSONPointer(settings.claims_map.display_name).resolve(payload),
        first_name=JSONPointer(settings.claims_map.first_name).resolve(payload),
        surname=JSONPointer(settings.claims_map.surname).resolve(payload),
        role=JSONPointer(settings.claims_map.role).resolve(payload),
        roles=JSONPointer(settings.claims_map.roles).resolve(payload),
    )
    return user
