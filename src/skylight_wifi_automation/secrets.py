from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
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


def aad_for(name: str) -> bytes:
    return b"swa:v1:" + name.encode()


def encrypt(key: bytes, plaintext: str, name: str) -> tuple[bytes, bytes]:
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aes.encrypt(nonce, plaintext.encode(), aad_for(name))
    return nonce, ciphertext


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, name: str) -> tuple[str, bool]:
    """Return the plaintext and whether the row still lacks associated data.

    New rows are bound to the secret name. A row written before that binding
    still decrypts, and the caller re-encrypts it.
    """
    aes = AESGCM(key)
    try:
        return aes.decrypt(nonce, ciphertext, aad_for(name)).decode(), False
    except InvalidTag:
        pass
    return aes.decrypt(nonce, ciphertext, None).decode(), True
