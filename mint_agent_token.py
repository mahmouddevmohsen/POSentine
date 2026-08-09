# -*- coding: utf-8 -*-
"""
mint_agent_token.py — one-off utility
================================================================
Mints a long-lived JWT for the agent, signed with the Supabase JWT secret.

This exists so that `service_role` never reaches the customer machine.
service_role bypasses RLS entirely; this token does not — it carries a
tenant_id claim that the policies in schema.sql check
(`auth.jwt() ->> 'tenant_id'`), so a stolen agent token can only ever
touch one tenant's rows.

    python mint_agent_token.py --tenant-id <uuid>
    SUPABASE_JWT_SECRET=... python mint_agent_token.py --tenant-id <uuid>

The secret is read from the environment or from stdin. It is never written
to a file, never echoed, and never logged. Only the token reaches stdout,
so the output can be piped straight into config.json without the secret
ever touching disk.

HS256 by hand rather than PyJWT: this runs once, and a one-off tool is not
worth a dependency on the machine that holds the money data.

⚠️ Rotating the Supabase JWT secret invalidates every token minted here.
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

ROLE = "authenticated"
AUDIENCE = "authenticated"
ISSUER = "supabase"
YEAR_SECONDS = 365 * 24 * 3600
DEFAULT_YEARS = 5

ENV_SECRET = "SUPABASE_JWT_SECRET"


def _b64url(raw: bytes) -> str:
    """JWT segments are base64url with the padding stripped."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segment(obj: dict) -> str:
    # separators matter: a JWT should not carry incidental whitespace
    return _b64url(json.dumps(obj, separators=(",", ":"),
                              sort_keys=True).encode("utf-8"))


def mint(secret: str, tenant_id: str, issued_at: _dt.datetime,
         years: int = DEFAULT_YEARS) -> str:
    """
    Pure. No clock, no randomness — the same inputs give the same token,
    which is what makes this testable at all.

    issued_at must be timezone-aware: an ambiguous exp on a five-year
    credential is not something to shrug at.
    """
    if not secret:
        raise ValueError(f"no JWT secret supplied (set {ENV_SECRET} or use stdin)")
    try:
        uuid.UUID(tenant_id)
    except (ValueError, AttributeError, TypeError):
        # A typo here mints a token that authenticates fine and matches no
        # rows — the agent would run for weeks uploading into nothing.
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
    """
    Read a JWT's claims without verifying the signature.

    For inspection only — we are checking what a token *says about itself*
    before we ship it, not trusting it. Verification is the server's job.

    This exists because `'service_role' in token` is a check that cannot
    work: a JWT is base64, so the literal string never appears, and the
    test returns False for an actual service_role key. A safety check that
    always passes is worse than no check.
    """
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(raw)
    except (IndexError, ValueError, TypeError) as exc:
        raise ValueError(f"not a readable JWT: {exc}") from None
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


def assert_is_agent_token(token: str, expected_tenant_id: str) -> None:
    """
    Refuse anything that is not a tenant-scoped agent token.

    Both failures are silent otherwise: a service_role key bypasses every
    access rule on a machine we do not control, and a token minted for the
    wrong tenant authenticates perfectly, matches no rows, and uploads into
    nothing for weeks.
    """
    claims = decode_claims(token)

    role = claims.get("role")
    if role != ROLE:
        raise ValueError(
            f"agent token has role={role!r}, must be {ROLE!r}. "
            "A service_role key bypasses every access rule and must never "
            "exist on the customer machine."
        )

    tenant = claims.get("tenant_id")
    if not tenant:
        raise ValueError(
            "agent token carries no tenant_id claim — RLS would match "
            "nothing and every upload would silently affect zero rows"
        )
    if tenant != expected_tenant_id:
        raise ValueError(
            f"agent token is for tenant {tenant}, but config says "
            f"{expected_tenant_id}. It would authenticate and match nothing."
        )


def _read_secret() -> str:
    """Environment first, then stdin. Never a file, never a CLI argument
    (arguments show up in process listings and shell history)."""
    secret = os.environ.get(ENV_SECRET, "").strip()
    if secret:
        return secret
    if sys.stdin.isatty():
        print(f"{ENV_SECRET} not set. Paste the Supabase JWT secret "
              "(input is not echoed to a file):", file=sys.stderr)
    return sys.stdin.readline().strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint a long-lived agent JWT.")
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
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        expires = _dt.datetime.fromtimestamp(claims["exp"], _dt.timezone.utc)
        print(json.dumps(claims, indent=2), file=sys.stderr)
        print(f"expires {expires.isoformat()}", file=sys.stderr)

    # stdout carries the token and nothing else, so it can be piped.
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
