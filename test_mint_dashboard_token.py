# -*- coding: utf-8 -*-
"""Tests for mint_dashboard_token.py — pure JWT minting + guards."""
import datetime as _dt

import pytest

import mint_dashboard_token as mdt

TENANT = "57b61b47-a590-49fe-803c-0c174a07b7ec"
SECRET = "unit-test-secret-not-real"
ISSUED = _dt.datetime(2026, 8, 16, 12, 0, tzinfo=_dt.timezone.utc)


def test_mint_claims_shape():
    tok = mdt.mint(SECRET, TENANT, ISSUED)
    c = mdt.decode_claims(tok)
    assert c["role"] == "dashboard_ro"
    assert c["tenant_id"] == TENANT
    assert c["aud"] == "authenticated"
    assert c["iss"] == "supabase"
    assert c["iat"] == int(ISSUED.timestamp())
    assert c["exp"] == c["iat"] + 5 * 365 * 24 * 3600


def test_mint_deterministic():
    a = mdt.mint(SECRET, TENANT, ISSUED)
    b = mdt.mint(SECRET, TENANT, ISSUED)
    assert a == b


def test_mint_requires_secret():
    with pytest.raises(ValueError):
        mdt.mint("", TENANT, ISSUED)


def test_mint_requires_valid_uuid():
    with pytest.raises(ValueError):
        mdt.mint(SECRET, "not-a-uuid", ISSUED)


def test_mint_requires_tz_aware_iat():
    with pytest.raises(ValueError):
        mdt.mint(SECRET, TENANT, _dt.datetime(2026, 8, 16, 12, 0))


def test_guard_accepts_dashboard_token():
    tok = mdt.mint(SECRET, TENANT, ISSUED)
    mdt.assert_is_dashboard_token(tok, TENANT)  # no raise


def test_guard_rejects_agent_role():
    # a token with role=authenticated must be refused by the dashboard guard
    import mint_agent_token as mat
    agent = mat.mint(SECRET, TENANT, ISSUED)
    with pytest.raises(ValueError):
        mdt.assert_is_dashboard_token(agent, TENANT)


def test_guard_rejects_wrong_tenant():
    tok = mdt.mint(SECRET, TENANT, ISSUED)
    other = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError):
        mdt.assert_is_dashboard_token(tok, other)


def test_guard_rejects_missing_tenant():
    import base64
    import json

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"role": "dashboard_ro", "aud": "authenticated", "iss": "supabase"}
    seg = lambda o: base64.urlsafe_b64encode(
        json.dumps(o, separators=(",", ":")).encode()).rstrip(b"=").decode()
    forged = f"{seg(header)}.{seg(payload)}.AAAA"
    with pytest.raises(ValueError):
        mdt.assert_is_dashboard_token(forged, TENANT)
