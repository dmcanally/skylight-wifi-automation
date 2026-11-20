from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_NAMES = ("SKYLIGHT_EMAIL", "SKYLIGHT_PASSWORD", "UNIFI_API_KEY")


@dataclass
class ResolvedSecrets:
    email: str
    password: str
    api_key: str
    sources: dict[str, str]

    def __repr__(self) -> str:
        return f"ResolvedSecrets(sources={self.sources!r})"

    def __str__(self) -> str:
        return repr(self)

    def materials(self) -> list[str]:
        return [value for value in (self.email, self.password, self.api_key) if len(value) >= 4]


def encrypt(key: bytes, plaintext: str) -> tuple[bytes, bytes]:
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aes.encrypt(nonce, plaintext.encode(), None)
    return nonce, ciphertext


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> str:
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()
