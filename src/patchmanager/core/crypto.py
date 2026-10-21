"""Symmetric encryption for secrets stored in the database (MFA seeds, tokens).

Keys come from ``PATCHMANAGER_ENCRYPTION_KEYS``: comma separated Fernet keys,
newest first. Older keys keep decrypting until data is re-encrypted.
"""

from functools import cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


@cache
def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in settings.PATCHMANAGER_ENCRYPTION_KEYS])


def encrypt(value: str) -> str:
    """Encrypt and authenticate ``value``."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a value produced by :func:`encrypt`; raises on tampering."""
    return _fernet().decrypt(token.encode()).decode()


def encrypt_bytes(value: bytes) -> bytes:
    """Encrypt file content (uploads at rest)."""
    return _fernet().encrypt(value)


def decrypt_bytes(token: bytes) -> bytes:
    """Decrypt file content; raises on tampering."""
    return _fernet().decrypt(token)
