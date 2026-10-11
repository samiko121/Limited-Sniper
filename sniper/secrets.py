"""Resolving cookies / tokens and keeping them out of logs.

A secret in config.json can be written three ways:
  "_|WARNING:-DO-NOT-SHARE-THIS..."  the raw value (simple, least safe)
  "env:MY_VARIABLE"                  read from an environment variable
  "keyring:buy"                      read from the OS credential store
                                     (Windows Credential Manager / macOS Keychain /
                                     Linux Secret Service). Set it with
                                     `python main.py set-secret buy`.
"""

from __future__ import annotations

import logging
import os
import re

SERVICE = "limited-sniper"
COOKIE_RE = re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS\.[^\s\"';,]+")

_known: set[str] = set()


class SecretError(Exception):
    pass


def register(secret: str | None) -> None:
    """Remember a secret so the log filter can mask it anywhere it shows up."""
    if secret and len(secret) >= 8:
        _known.add(secret)


def mask(text: str) -> str:
    text = COOKIE_RE.sub("<cookie>", text)
    for s in _known:
        if s in text:
            text = text.replace(s, "<secret>")
    return text


def resolve(value: str | None, what: str) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("env:"):
        name = value[4:]
        secret = os.environ.get(name)
        if not secret:
            raise SecretError(f"{what}: environment variable {name} is not set")
    elif value.startswith("keyring:"):
        name = value[8:]
        try:
            import keyring
            secret = keyring.get_password(SERVICE, name)
        except Exception as e:  # keyring backend missing / locked
            raise SecretError(f"{what}: could not read '{name}' from the OS credential store ({e})") from None
        if not secret:
            raise SecretError(f"{what}: nothing stored under '{name}'. Run: python main.py set-secret {name}")
    else:
        secret = value
    register(secret)
    return secret


def store(name: str, secret: str) -> None:
    import keyring
    keyring.set_password(SERVICE, name, secret)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = mask(msg)
        if masked != msg:
            record.msg, record.args = masked, None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        return True
