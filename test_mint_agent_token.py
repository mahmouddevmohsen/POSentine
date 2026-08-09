# -*- coding: utf-8 -*-
"""
test_mint_agent_token.py — the token the customer machine will carry.

This token replaces service_role on a machine we do not control, so its
shape is a security boundary rather than a detail. Minting is a pure
function of (secret, tenant_id, issued_at) so it can be pinned exactly —
no clock, no randomness, same inputs give the same token.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

import pytest

import mint_agent_token as mint

SECRET = "test-secret-not-the-real-one"
TENANT = "57b61b47-a590-49fe-803c-0c174a07b7ec"
IAT = dt.datetime(2026, 8, 9, 0, 0, 0, tzinfo=dt.timezone.utc)


def b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def parts(token: str):
    head, payload, sig = token.split(".")
    return json.loads(b64url_decode(head)), json.loads(b64url_decode(payload)), sig


# ════════════════════════════════════════════════════════════════
# shape
# ════════════════════════════════════════════════════════════════

def test_token_has_three_segments():
    assert mint.mint(SECRET, TENANT, IAT).count(".") == 2


def test_header_is_hs256():
    """Supabase legacy projects sign symmetrically."""
    head, _, _ = parts(mint.mint(SECRET, TENANT, IAT))
    assert head == {"alg": "HS256", "typ": "JWT"}


def test_base64url_is_unpadded():
    """'=' padding is not valid in a JWT segment."""
    assert "=" not in mint.mint(SECRET, TENANT, IAT)


# ════════════════════════════════════════════════════════════════
# signature
# ════════════════════════════════════════════════════════════════

def test_signature_verifies_against_the_secret():
    token = mint.mint(SECRET, TENANT, IAT)
    signing_input, sig = token.rsplit(".", 1)
    expected = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode("utf-8"), signing_input.encode("ascii"),
                 hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert hmac.compare_digest(sig, expected)


def test_signature_does_not_verify_against_a_different_secret():
    token = mint.mint(SECRET, TENANT, IAT)
    signing_input, sig = token.rsplit(".", 1)
    wrong = base64.urlsafe_b64encode(
        hmac.new(b"other-secret", signing_input.encode("ascii"),
                 hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    assert sig != wrong


# ════════════════════════════════════════════════════════════════
# claims
# ════════════════════════════════════════════════════════════════

def test_claims_carry_role_and_tenant():
    """RLS reads auth.jwt() ->> 'tenant_id' (schema.sql:353)."""
    _, payload, _ = parts(mint.mint(SECRET, TENANT, IAT))
    assert payload["role"] == "authenticated"
    assert payload["tenant_id"] == TENANT
    assert payload["aud"] == "authenticated"


def test_expiry_is_five_years_by_default():
    _, payload, _ = parts(mint.mint(SECRET, TENANT, IAT))
    assert payload["iat"] == int(IAT.timestamp())
    assert payload["exp"] - payload["iat"] == 5 * 365 * 24 * 3600


def test_expiry_is_configurable():
    _, payload, _ = parts(mint.mint(SECRET, TENANT, IAT, years=1))
    assert payload["exp"] - payload["iat"] == 365 * 24 * 3600


def test_role_is_never_service_role():
    """service_role bypasses all RLS and must never reach the customer."""
    _, payload, _ = parts(mint.mint(SECRET, TENANT, IAT))
    assert "service_role" not in json.dumps(payload)


# ════════════════════════════════════════════════════════════════
# purity and refusals
# ════════════════════════════════════════════════════════════════

def test_minting_is_deterministic():
    """No clock, no randomness — the same inputs give the same token."""
    assert mint.mint(SECRET, TENANT, IAT) == mint.mint(SECRET, TENANT, IAT)


def test_secret_never_appears_in_the_token():
    assert SECRET not in mint.mint(SECRET, TENANT, IAT)


def test_empty_secret_is_refused():
    with pytest.raises(ValueError):
        mint.mint("", TENANT, IAT)


def test_non_uuid_tenant_is_refused():
    """A typo'd tenant_id mints a token that silently matches no rows."""
    with pytest.raises(ValueError):
        mint.mint(SECRET, "not-a-uuid", IAT)


def test_naive_issued_at_is_refused():
    """An ambiguous exp on a five-year credential is not acceptable."""
    with pytest.raises(ValueError):
        mint.mint(SECRET, TENANT, dt.datetime(2026, 8, 9, 0, 0, 0))
