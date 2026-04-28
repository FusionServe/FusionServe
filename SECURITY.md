# Security Policy

## Reporting a vulnerability

If you believe you have discovered a security vulnerability in FusionServe,
please report it privately by emailing **fras.marco@gmail.com**. Do not file
public GitHub issues for security-sensitive reports.

When reporting, please include:

- A description of the issue and its impact.
- Steps to reproduce, ideally with a minimal proof of concept.
- The affected version or commit.
- Any suggested mitigation, if known.

You can expect an acknowledgement within five business days. We will keep
you informed of remediation progress and coordinate a disclosure timeline
with you before publishing details.

## Supported versions

FusionServe is currently pre-1.0. Only the latest minor version receives
security fixes. Once a 1.0 line is published, the support window will be
documented here.

## Hardening notes

- Database credentials live in environment variables (or a developer-local
  `.env` file). The repository ships an `.env.example` template; never
  commit a real `.env`.
- The application authenticates JSON Web Tokens against a JWKS endpoint
  (resolved via OIDC discovery if not configured directly). RS256 is the
  only signing algorithm accepted.
- Per-request PostgreSQL role switching uses `SET ROLE` plus row-level
  security in the database. Do not introduce code paths that open a
  database session without calling `persistence.set_role` first.
