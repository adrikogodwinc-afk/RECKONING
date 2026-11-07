"""Credential vault and device tokens.

On Windows, secrets are encrypted with DPAPI (bound to the Windows user, no key
file to steal). Off Windows (tests only) a Fernet key file is used.
Passwords are never logged and never returned by any API.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import time

IS_WINDOWS = sys.platform == "win32"


class Vault:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self._fernet = None
        if not IS_WINDOWS:
            from cryptography.fernet import Fernet
            keyfile = os.path.join(directory, "test.key")
            if not os.path.exists(keyfile):
                with open(keyfile, "wb") as fh:
                    fh.write(Fernet.generate_key())
                os.chmod(keyfile, 0o600)
            self._fernet = Fernet(open(keyfile, "rb").read())

    def _encrypt(self, data: bytes) -> bytes:
        if IS_WINDOWS:
            import win32crypt  # type: ignore
            return win32crypt.CryptProtectData(data, "TradingOps", None, None, None, 0)
        return self._fernet.encrypt(data)

    def _decrypt(self, blob: bytes) -> bytes:
        if IS_WINDOWS:
            import win32crypt  # type: ignore
            return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
        return self._fernet.decrypt(blob)

    def store(self, secret: str) -> str:
        ref = secrets.token_hex(16)
        with open(os.path.join(self.dir, ref + ".bin"), "wb") as fh:
            fh.write(self._encrypt(secret.encode("utf-8")))
        return ref

    def load(self, ref: str) -> str:
        if not ref.isalnum():
            raise ValueError("bad reference")
        with open(os.path.join(self.dir, ref + ".bin"), "rb") as fh:
            return self._decrypt(fh.read()).decode("utf-8")

    def revoke(self, ref: str) -> None:
        if not ref.isalnum():
            raise ValueError("bad reference")
        path = os.path.join(self.dir, ref + ".bin")
        if os.path.exists(path):
            os.remove(path)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Pairing:
    """One-time pairing codes shown on the PC screen and typed into the phone."""

    def __init__(self, ttl_s: int = 300, max_tries: int = 5) -> None:
        self.ttl_s, self.max_tries = ttl_s, max_tries
        self._code: str | None = None
        self._expires = 0.0
        self._tries = 0

    def new_code(self) -> str:
        self._code = f"{secrets.randbelow(10**6):06d}"
        self._expires = time.monotonic() + self.ttl_s
        self._tries = 0
        return self._code

    def redeem(self, code: str) -> str | None:
        """Returns a new device token on success. Codes are single-use and rate-limited."""
        if self._code is None or time.monotonic() > self._expires:
            return None
        self._tries += 1
        if self._tries > self.max_tries:
            self._code = None
            return None
        if hmac.compare_digest(code, self._code):
            self._code = None
            return secrets.token_urlsafe(32)
        return None


def redact(text: str, secrets_to_hide: list[str]) -> str:
    for s in secrets_to_hide:
        if s:
            text = text.replace(s, "***")
    return text
