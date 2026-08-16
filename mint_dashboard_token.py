# -*- coding: utf-8 -*-
"""
mint_dashboard_token.py — one-off utility
================================================================
Mints a long-lived JWT for the dashboard (owner-facing read-only UI),
signed with the Supabase JWT secret. The token carries:

    role: dashboard_ro      → a Postgres role with SELECT-only grants
                              (schema_v8_dashboard_ro.sql) — a stolen
                              token can read one tenant's reporting
                              tables and nothing else.
    tenant_id: <uuid>       → RLS policy (dashboard_ro_select) filters
                              every read to this tenant's rows.

Mirrors mint_agent_token.py exactly: same HS256-by-hand, same secret
handling (env or stdin, never a file), same never-echo discipline. The
two tokens differ only in the role claim, which is the whole point —
the agent may write ingestion tables, the dashboard may read reporting
tables, and neither can do the other's job.

    python mint_dashboard_token.py --tenant-id <uuid>
    SUPABASE_JWT_SECRET=... python mint_dashboard_token.py --tenant-id <uuid>

⚠️ Rotating the Supabase JWT secret invalidates every token minted here.
⚠️ Requires schema_v8_dashboard_ro.sql applied first — without the role,
   PostgREST answers 42501 (role does not exist) for every read.
================================================================
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import sys
import uuid

ROLE = "dashboard_ro"
AUDIENCE = "authenticated"
ISSUER = "supabase"
YEAR_SECONDS = 365 * 24 * 3600
DEFAULT_YEARS = 5

ENV_SECRET = "SUPABASE_JWT_SECRET"


def _b64url(raw: bytes) -> str:
    """JWT segments are base64url with the padding stripped."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(obj: dict) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":"),
                              sort_keys=True).encode("utf-8"))


def mint(secret: str, tenant_id: str, issued_at: _dt.datetime,
         years: int = DEFAULT_YEARS) -> str:
    """Pure. Same inputs → same token, which is what makes this testable."""
    if not secret:
        raise ValueError(f"no JWT secret supplied (set {ENV_SECRET} or use stdin)")
    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"tenant_id must be a UUID, got {tenant_id!r}") from None
    if issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")

    iat = int(issued_at.timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "role": ROLE,
        "tenant_id": tenant_id,
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": iat,
        "exp": iat + years * YEAR_SECONDS,
    }

    signing_input = f"{_segment(header)}.{_segment(payload)}"
    signature = hmac.new(secret.encode("utf-8"),
                         signing_input.encode("ascii"),
                         hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_claims(token: str) -> dict:
    """Read a JWT's claims without verifying the signature (inspection only)."""
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (IndexError, ValueError, TypeError) as exc:
        raise ValueError(f"not a readable JWT: {exc}") from None
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


def assert_is_dashboard_token(token: str, expected_tenant_id: str) -> None:
    """
    Refuse anything that is not a read-only dashboard token for this tenant.

    A service_role key or an agent token (role=authenticated) must never
    sit in the dashboard's config — one bypasses every rule, the other can
    write ingestion tables. Only role=dashboard_ro + this tenant's claim
    is acceptable here.
    """
    claims = decode_claims(token)

    role = claims.get("role")
    if role != ROLE:
        raise ValueError(
            f"dashboard token has role={role!r}, must be {ROLE!r}. "
            "An agent token (authenticated) can write ingestion tables and "
            "must never be used by the dashboard; service_role bypasses RLS."
        )

    tenant = claims.get("tenant_id")
    if not tenant:
        raise ValueError(
            "dashboard token carries no tenant_id claim — RLS would match "
            "nothing and every read would silently return zero rows"
        )
    if tenant != expected_tenant_id:
        raise ValueError(
            f"dashboard token is for tenant {tenant}, but config says "
            f"{expected_tenant_id}. It would authenticate and match nothing."
        )


def _read_secret() -> str:
    secret = os.environ.get(ENV_SECRET, "").strip()
    if secret:
        return secret
    if sys.stdin.isatty():
        print(f"{ENV_SECRET} not set. Paste the Supabase JWT secret "
              "(input is not echoed to a file):", file=sys.stderr)
    return sys.stdin.readline().strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint a long-lived dashboard JWT.")
    ap.add_argument("--tenant-id", required=True)
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--show-claims", action="store_true",
                    help="print the decoded claims to stderr for inspection")
    args = ap.parse_args(argv)

    try:
        token = mint(_read_secret(), args.tenant_id,
                     _dt.datetime.now(_dt.timezone.utc), args.years)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.show_claims:
        claims = decode_claims(token)
        expires = _dt.datetime.fromtimestamp(claims["exp"], _dt.timezone.utc)
        print(json.dumps(claims, indent=2), file=sys.stderr)
        print(f"expires {expires.isoformat()}", file=sys.stderr)

    # stdout carries the token and nothing else, so it can be piped.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
