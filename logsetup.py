# -*- coding: utf-8 -*-
"""
logsetup.py — the log is the only way back into this machine
================================================================
Assume something misbehaves three weeks after we leave, nobody was
watching, and the only way in is a file. Everything here exists for that
reader.

Three properties, each of which fails silently if it is left to habit:

  • **Bounded.** A log that fills a till's disk is a fault we caused.
    Size-capped, a fixed number of files, and the ceiling is arithmetic
    rather than a hope: (LOG_BACKUP_COUNT + 1) x LOG_MAX_BYTES.

  • **No secret, ever.** Redaction happens in the *formatter*, so it
    applies to the message, the arguments and the exception traceback
    alike, on every handler, whether or not the person writing the log
    line remembered. Remembering is not a mechanism.

  • **Readable by us, later.** One line per event, timestamped, with the
    numbers on it. "what was this machine doing at 03:14 last Tuesday"
    has to be answerable from the file with no debugger and no shell.

Redaction detail worth knowing: a secret is masked whole, and also from
its first 12 characters onwards. Error text gets truncated on its way to
the cloud (`detail[:500]`) and by terminal wrapping, and a half-printed
token is exactly as leaked as a whole one.
================================================================
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Mapping

# 2 MiB x 6 files = a 12 MiB ceiling on a machine we cannot reach.
# For scale: a healthy cycle writes ~4 short lines every 3 minutes, about
# 2 KB a day, so a quiet till never reaches the first rollover. A machine
# stuck in an error loop writes a traceback per cycle — roughly 500 KB a
# day — and still keeps three weeks of history inside the ceiling, which
# is the window that decides whether week three costs a trip.
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"

# Keys whose *value* is a secret. Matched as substrings of the key name so
# a field added later — "service_token", "api_key" — is covered without
# anyone remembering to come back here.
SECRET_KEY_HINTS = ("password", "secret", "token", "key", "pwd", "passwd")

# Below this a value cannot be masked without shredding the log: a
# two-character password would blank every occurrence of those two
# characters everywhere. Reported loudly rather than skipped quietly.
MIN_MASKABLE = 4

# How much of a secret has to appear before the rest is assumed to follow.
# 12 characters of a JWT or a password is already a leak, and it is far
# past any accidental collision with ordinary log text.
HEAD_CHARS = 12

# The character classes a credential runs through: base64url for JWTs,
# plus what turns up in SQL passwords.
_TAIL = r"[A-Za-z0-9+/=._~\-]*"

_SECRETS: list[tuple[str, str]] = []
_SHORT_SECRETS: list[str] = []


# ════════════════════════════════════════════════════════════════
# what counts as a secret
# ════════════════════════════════════════════════════════════════

def secret_values(raw: Mapping[str, Any],
                  prefix: str = "") -> list[tuple[str, str]]:
    """Walk a config and return [(dotted label, value)] for every secret.

    The label is what appears in the log in place of the value, so a
    reader can tell *which* credential was there — "***sql.password***"
    is a fact; a row of asterisks is a puzzle.
    """
    found: list[tuple[str, str]] = []
    for key, value in raw.items():
        label = f"{prefix}{key}"
        if isinstance(value, Mapping):
            found.extend(secret_values(value, prefix=f"{label}."))
            continue
        if not isinstance(value, str) or not value:
            continue
        if any(hint in key.lower() for hint in SECRET_KEY_HINTS):
            found.append((label, value))
    return found


def register_secret(label: str, value: str) -> bool:
    """Add one value to the mask list. Returns False when it is too short
    to mask safely — the caller is expected to say so out loud."""
    if not value:
        return False
    if len(value) < MIN_MASKABLE:
        if label not in _SHORT_SECRETS:
            _SHORT_SECRETS.append(label)
        return False
    if (label, value) not in _SECRETS:
        _SECRETS.append((label, value))
    return True


def register_config_secrets(raw: Mapping[str, Any]) -> list[str]:
    """Register every secret in a loaded config. Returns the labels that
    could NOT be masked, so the caller can warn about them."""
    unmaskable: list[str] = []
    for label, value in secret_values(raw):
        if not register_secret(label, value):
            unmaskable.append(label)
    return unmaskable


def registered_labels() -> list[str]:
    return [label for label, _ in _SECRETS]


def forget_secrets() -> None:
    """Tests only. Nothing in the product ever un-registers a secret."""
    _SECRETS.clear()
    _SHORT_SECRETS.clear()


# ════════════════════════════════════════════════════════════════
# masking
# ════════════════════════════════════════════════════════════════

def mask(text: str) -> str:
    """Replace every registered secret, whole or truncated.

    Longest first: if one credential happens to contain another, masking
    the short one first would leave the remainder of the long one on the
    line.
    """
    if not text or not _SECRETS:
        return text
    for label, value in sorted(_SECRETS, key=lambda s: len(s[1]), reverse=True):
        placeholder = f"***{label}***"
        if value in text:
            text = text.replace(value, placeholder)
        if len(value) > HEAD_CHARS:
            head = value[:HEAD_CHARS]
            if head in text:
                text = re.sub(re.escape(head) + _TAIL, placeholder, text)
    return text


class MaskingFormatter(logging.Formatter):
    """Redaction belongs here and not at the call sites.

    A `LOG.exception(...)` renders its traceback inside the formatter, and
    a traceback is exactly where a connection string or a token turns up
    unannounced. Masking the finished string is the only point that sees
    all of it.
    """

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


# ════════════════════════════════════════════════════════════════
# the rolling file
# ════════════════════════════════════════════════════════════════

class CappedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that survives a rollover it cannot perform.

    agent.py runs one cycle per invocation, and the stale-lock takeover
    deliberately allows two processes to overlap after a hang. On Windows
    a rename fails while another process holds the file open. Raising
    there would take down a cycle to tidy a log file; dropping the record
    would lose the line that explains the hang.

    So a failed rollover is written into the log itself and skipped. The
    next cycle — three minutes later, on its own — rotates instead. The
    ceiling is therefore enforced on the next successful write rather
    than on this one, which is stated here rather than implied.
    """

    def doRollover(self) -> None:                  # noqa: N802 (stdlib name)
        try:
            super().doRollover()
        except OSError as exc:
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(
                f"{'':19} WARNING rotation skipped, another process holds "
                f"the log ({type(exc).__name__}); the next cycle rotates\n")
            self.stream.flush()


def log_files(log_path: Path) -> list[Path]:
    """Every file this handler owns: the live one plus its backups."""
    candidates = [log_path] + [log_path.with_name(f"{log_path.name}.{n}")
                               for n in range(1, LOG_BACKUP_COUNT + 1)]
    return [p for p in candidates if p.exists()]


def total_log_bytes(log_path: Path) -> int:
    return sum(p.stat().st_size for p in log_files(log_path))


# ════════════════════════════════════════════════════════════════
# wiring
# ════════════════════════════════════════════════════════════════

def configure_streams() -> None:
    """Arabic item names on a default Windows console are cp1252, and
    printing one raises UnicodeEncodeError. errors='replace' deliberately:
    garbled output can be read past, a crash during an install cannot."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def configure(log_path: Path | None = None,
              level: int = logging.INFO,
              extra_handlers: Iterable[logging.Handler] = ()) -> None:
    """Console + optional rolling file, both masked.

    Called before the config is read, so the secret list is usually empty
    at this point. That is fine: MaskingFormatter consults the list when
    it formats, not when it is built, and Config.load registers the
    secrets before anything can log one.
    """
    configure_streams()

    formatter = MaskingFormatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # The Scheduled Task runs hidden, so this file is the only output
        # that survives. Opened UTF-8 explicitly for the same reason.
        handlers.append(CappedRotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8"))
    handlers.extend(extra_handlers)

    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)

    for label in _SHORT_SECRETS:
        # Loud, because the alternative is a credential in a log we send
        # ourselves. Not fatal, because refusing to start over a short
        # password would strand an install at the counter.
        logging.getLogger("posentine.logsetup").error(
            "%s is shorter than %d characters and is NOT redacted from these "
            "logs. Change it, or treat every log from this machine as "
            "carrying it.", label, MIN_MASKABLE)
