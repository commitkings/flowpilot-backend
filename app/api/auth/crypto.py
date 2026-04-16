"""Symmetric encryption helpers for storing sensitive values at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from JWT_SECRET.
"""

import base64
import hashlib

from cryptography.fernet import Fernet

from src.config.settings import Settings


def _fernet() -> Fernet:
    key_bytes = hashlib.sha256(Settings.JWT_SECRET.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt_value(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
