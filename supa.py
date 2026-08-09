# -*- coding: utf-8 -*-
"""
supa.py — Supabase REST (PostgREST) client
================================================================
HTTPS/443 only. Direct Postgres ports are often blocked on the customer
network, so every call goes through the REST gateway.

Two headers, always:
    apikey:        <anon key>          gateway admission
    Authorization: Bearer <token>      carries tenant_id for RLS

Three behaviours here exist to prevent *silent* wrongness:

  1. select() paginates. PostgREST caps at 1000 rows and says nothing.
     A month of invoice_lines is ~35k rows; truncation would render a
     confident, wrong top-5.
  2. A caller that opts out of pagination and lands exactly on the page
     limit gets a TruncationError, because "1000 rows" and "the first
     1000 of an unknown number" are indistinguishable.
  3. A partial batch write raises and reports how many rows landed.
     The sync watermark advances on success only; a half-written upload
     that returned success would skip real invoices forever.
================================================================
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Mapping, Sequence

import requests

DEFAULT_PAGE = 1000          # PostgREST's own default ceiling
DEFAULT_BATCH = 500          # rows per write request
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SupaError(RuntimeError):
    """Any REST failure. Never carries a credential in its message."""


class TruncationError(SupaError):
    """A result set may have been cut off and we cannot tell."""


def _decode(data: bytes | str | None) -> list[dict[str, Any]]:
    """Body bytes back to rows. Used by callers and by the tests."""
    if not data:
        return []
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def _encode(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")


class Supa:
    def __init__(
        self,
        base_url: str,
        anon_key: str,
        token: str,
        session: Any | None = None,
        sleep: Any | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout: int = DEFAULT_TIMEOUT,
        page: int = DEFAULT_PAGE,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(
                "base_url must be https:// — plain HTTP is not acceptable "
                f"for this data (got {base_url.split('://')[0]!r})"
            )
        self.base_url = base_url.rstrip("/")
        self._anon_key = anon_key
        self._token = token
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep if sleep is not None else time.sleep
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.page = page
        self.batch = batch

    # ── credential hygiene ──────────────────────────────────────

    def __repr__(self) -> str:
        return f"<Supa {self.base_url} token=***>"

    def _redact(self, text: str) -> str:
        """Rule 9: never log secrets. Applied to every error surface."""
        for secret in (self._token, self._anon_key):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        h = {
            "apikey": self._anon_key,
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    # ── transport ───────────────────────────────────────────────

    def _request(self, method: str, table: str,
                 headers: Mapping[str, str] | None = None,
                 params: Mapping[str, str] | None = None,
                 data: bytes | None = None):
        url = f"{self.base_url}/rest/v1/{table}"
        last: str = ""

        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.request(
                    method, url,
                    headers=self._headers(headers),
                    params=dict(params or {}),
                    data=data,
                    timeout=self.timeout,
                )
            except Exception as exc:                  # transport-level failure
                last = f"{type(exc).__name__}: {self._redact(str(exc))}"
                if attempt == self.max_attempts:
                    break
                self.sleep(self._backoff(attempt))
                continue

            if 200 <= resp.status_code < 300:
                return resp

            body = self._redact(getattr(resp, "text", "") or "")
            last = f"HTTP {resp.status_code} {body}".strip()

            # A 403 from RLS is an answer, not a hiccup. Retrying hides it.
            if resp.status_code not in RETRY_STATUSES:
                raise SupaError(f"{method} {table} failed: {last}")

            if attempt == self.max_attempts:
                break
            self.sleep(self._retry_delay(resp, attempt))

        raise SupaError(
            f"{method} {table} failed after {self.max_attempts} attempts: {last}"
        )

    def _backoff(self, attempt: int) -> float:
        return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)

    def _retry_delay(self, resp, attempt: int) -> float:
        """429 states its own wait; anything else backs off exponentially."""
        if resp.status_code == 429:
            raw = (getattr(resp, "headers", {}) or {}).get("Retry-After")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return self._backoff(attempt)

    # ── reads ───────────────────────────────────────────────────

    def select(self, table: str,
               params: Mapping[str, str] | None = None,
               paginate: bool = True) -> list[dict[str, Any]]:
        """
        Read rows, following Range pages until the tail is short.

        paginate=False is for callers that genuinely want one page. If that
        page comes back exactly full, we cannot distinguish "all of it" from
        "the first 1000 of many", so we raise instead of guessing.
        """
        out: list[dict[str, Any]] = []
        offset = 0

        while True:
            resp = self._request(
                "GET", table,
                headers={"Range-Unit": "items",
                         "Range": f"{offset}-{offset + self.page - 1}"},
                params=params,
            )
            chunk = resp.json() or []
            if not isinstance(chunk, list):
                raise SupaError(f"GET {table}: expected a list of rows")
            out.extend(chunk)

            if not paginate:
                if len(chunk) == self.page:
                    raise TruncationError(
                        f"GET {table} returned exactly {self.page} rows with "
                        "pagination disabled — the result may be truncated and "
                        "there is no way to tell. Use paginate=True."
                    )
                return out

            if len(chunk) < self.page:
                return out
            offset += self.page

    # ── writes ──────────────────────────────────────────────────

    def _write_batches(self, table: str, rows: Sequence[Mapping[str, Any]],
                       prefer: str, params: Mapping[str, str]) -> int:
        if not rows:
            return 0

        landed = 0
        for start in range(0, len(rows), self.batch):
            part = rows[start:start + self.batch]
            try:
                self._request("POST", table,
                              headers={"Prefer": prefer},
                              params=params,
                              data=_encode(part))
            except SupaError as exc:
                # Partial success is failure. Say exactly how far we got so
                # the caller cannot mistake this for a completed upload.
                raise SupaError(
                    f"{table}: {landed} of {len(rows)} rows landed before "
                    f"the batch at offset {start} failed — {exc}"
                ) from exc
            landed += len(part)
        return landed

    def upsert(self, table: str, rows: Sequence[Mapping[str, Any]],
               on_conflict: str) -> int:
        """Insert or merge on the primary key. Returns rows written."""
        return self._write_batches(
            table, rows,
            prefer="resolution=merge-duplicates,return=minimal",
            params={"on_conflict": on_conflict},
        )

    def insert_ignore(self, table: str, rows: Sequence[Mapping[str, Any]],
                      on_conflict: str) -> int:
        """`on conflict do nothing` — how events are recorded."""
        return self._write_batches(
            table, rows,
            prefer="resolution=ignore-duplicates,return=minimal",
            params={"on_conflict": on_conflict},
        )

    def insert(self, table: str, rows: Sequence[Mapping[str, Any]],
               returning: bool = True) -> list[dict[str, Any]]:
        """Plain insert. Returns the created rows when returning=True."""
        if not rows:
            return []
        prefer = "return=representation" if returning else "return=minimal"
        resp = self._request("POST", table,
                             headers={"Prefer": prefer},
                             data=_encode(rows))
        return resp.json() if returning else []

    def update(self, table: str, filters: Mapping[str, str],
               patch: Mapping[str, Any],
               returning: bool = True) -> list[dict[str, Any]]:
        """PATCH rows matching PostgREST filters, e.g. {'salid': 'eq.42'}."""
        prefer = "return=representation" if returning else "return=minimal"
        body = json.dumps(patch, ensure_ascii=False, default=str).encode("utf-8")
        resp = self._request("PATCH", table,
                             headers={"Prefer": prefer},
                             params=filters,
                             data=body)
        return resp.json() if returning else []

    def delete(self, table: str, filters: Mapping[str, str]) -> None:
        self._request("DELETE", table,
                      headers={"Prefer": "return=minimal"},
                      params=filters)
