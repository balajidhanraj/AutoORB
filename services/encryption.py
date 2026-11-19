"""AES-256-GCM encryption service for broker passwords and secrets."""

from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 32-byte secret key derivation or retrieval from environment
_DEFAULT_KEY = os.getenv("APP_ENCRYPTION_KEY", "AutoORB_Secret_Key_32_Bytes_Long_2026!").encode("utf-8")[:32].ljust(32, b"0")


def encrypt_secret(plain_text: str, key: Optional[bytes] = None) -> str:
    """Encrypt sensitive string using AES-256-GCM returning Base64 string."""
    if not plain_text:
        return ""
    cipher_key = key or _DEFAULT_KEY
    aesgcm = AESGCM(cipher_key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    encrypted = aesgcm.encrypt(nonce, plain_text.encode("utf-8"), None)
    # Store nonce + ciphertext together
    return base64.b64encode(nonce + encrypted).decode("utf-8")


def decrypt_secret(cipher_b64: str, key: Optional[bytes] = None) -> str:
    """Decrypt Base64 AES-256-GCM ciphertext back to plain string."""
    if not cipher_b64:
        return ""
    cipher_key = key or _DEFAULT_KEY
    raw = base64.b64decode(cipher_b64.encode("utf-8"))
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(cipher_key)
    decrypted = aesgcm.decrypt(nonce, ciphertext, None)
    return decrypted.decode("utf-8")

