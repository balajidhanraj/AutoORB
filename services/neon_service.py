"""Neon services integration: Lakebase Postgres, Neon Managed Auth, and Neon Realtime."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
NEON_SEAL_SCRIPT = ROOT_DIR / "scripts" / "neon_seal.mjs"


def get_database_name() -> str:
    """Extract database name (e.g. 'neondb') from DATABASE_URL."""
    raw = os.getenv("DATABASE_URL", "")
    if not raw or "sqlite" in raw:
        return "neondb"
    try:
        parts = urlsplit(raw)
        return (parts.path or "/neondb").lstrip("/") or "neondb"
    except Exception:
        return "neondb"


def get_neon_config_summary() -> Dict[str, Any]:
    """Return non-secret summary of wired Neon services for health & public config endpoints."""
    db_url = os.getenv("DATABASE_URL", "")
    unpooled_url = os.getenv("DATABASE_URL_UNPOOLED", "")
    auth_base = os.getenv("NEON_AUTH_BASE_URL", "").strip()
    jwks_url = os.getenv("NEON_AUTH_JWKS_URL", "").strip()
    rt_url = os.getenv("NEON_REALTIME_URL", "").strip()
    rt_secret = os.getenv("NEON_REALTIME_SECRET", "").strip()

    is_neon_pg = "neon.tech" in db_url or ("postgresql" in db_url and "sqlite" not in db_url)
    return {
        "enabled": bool(is_neon_pg or auth_base or rt_url),
        "branch": os.getenv("NEON_BRANCH", "production"),
        "branch_id": os.getenv("NEON_BRANCH_ID", ""),
        "database": get_database_name(),
        "postgres_pooled": "-pooler." in db_url,
        "postgres_unpooled_configured": bool(unpooled_url),
        "auth_enabled": bool(auth_base and jwks_url),
        "auth_base_url": auth_base,
        "auth_jwks_url": jwks_url,
        "realtime_enabled": bool(rt_url and rt_secret),
        "realtime_url": rt_url,
    }


async def check_neon_auth_health() -> Dict[str, Any]:
    """Verify Neon Managed Auth JWKS and base endpoint reachability."""
    auth_base = os.getenv("NEON_AUTH_BASE_URL", "").strip()
    jwks_url = os.getenv("NEON_AUTH_JWKS_URL", "").strip()
    if not auth_base or not jwks_url:
        return {"ok": False, "reason": "NEON_AUTH_BASE_URL or NEON_AUTH_JWKS_URL not configured"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        jwks_resp = await client.get(jwks_url)
        jwks_resp.raise_for_status()
        jwks_data = jwks_resp.json()
        keys = jwks_data.get("keys") if isinstance(jwks_data, dict) else []
        return {
            "ok": True,
            "auth_base_url": auth_base,
            "jwks_url": jwks_url,
            "jwks_keys_count": len(keys or []),
        }


async def verify_neon_auth_session(session_token: str) -> Optional[Dict[str, Any]]:
    """Verify a Neon Managed Better Auth bearer token or session token against NEON_AUTH_BASE_URL."""
    auth_base = os.getenv("NEON_AUTH_BASE_URL", "").strip().rstrip("/")
    if not auth_base or not session_token:
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{auth_base}/get-session",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("user"):
                return data["user"]
    return None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_pg_text_param(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        raw = "true" if value else "false"
    else:
        raw = str(value)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _seal_neon_realtime_query_python(
    sql_query: str,
    params: Optional[List[Any]],
    rt_secret: str,
    db_name: str,
    rt_url: str,
) -> Dict[str, Any]:
    """Pure-Python Compact-JWE fallback matching @neon/realtime/server v2 format."""
    prefix = "nrt_live_1"
    if not rt_secret.startswith(prefix):
        raise ValueError("Invalid Realtime secret")
    key = _b64url_decode(rt_secret[len(prefix) :])
    if len(key) != 32:
        raise ValueError("Invalid Realtime secret key length")

    encoded_params = [
        {"type_oid": 0, "value": _encode_pg_text_param(p)} for p in (params or [])
    ]
    canonical = json.dumps(
        {"sql": sql_query, "parameters": encoded_params},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    query_fp = hashlib.sha256(b"neon-live-query-fingerprint-v1\x00" + canonical).hexdigest()

    issued_at = int(time.time())
    expires_at = issued_at + 60
    claims = {
        "v": 2,
        "aud": "realtime-proxy",
        "database": db_name,
        "error_details": "safe",
        "issuer": {
            "sdk": "javascript",
            "release_line": "0.2",
            "query_source": "raw_sql",
        },
        "query_fingerprint": query_fp,
        "sql": sql_query,
        "parameters": encoded_params,
        "iat": issued_at,
        "exp": expires_at,
    }
    encoded_header = _b64url_encode(
        json.dumps({"alg": "dir", "enc": "A256GCM", "v": 2}, separators=(",", ":")).encode("utf-8")
    )
    iv = os.urandom(12)
    encrypted = AESGCM(key).encrypt(
        iv,
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        encoded_header.encode("utf-8"),
    )
    ciphertext, tag = encrypted[:-16], encrypted[-16:]
    capability = ".".join(
        [
            encoded_header,
            "",
            _b64url_encode(iv),
            _b64url_encode(ciphertext),
            _b64url_encode(tag),
        ]
    )
    return {
        "ok": True,
        "db": db_name,
        "realtime_url": rt_url,
        "sealed_query": {
            "capability": capability,
            "queryFingerprint": query_fp,
            "expiresAt": expires_at * 1000,
        },
    }


def seal_neon_realtime_query(sql_query: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Seal a live SQL query with NEON_REALTIME_SECRET via @neon/realtime/server or pure-Python fallback."""
    rt_secret = os.getenv("NEON_REALTIME_SECRET", "").strip()
    rt_url = os.getenv("NEON_REALTIME_URL", "").strip()
    if not rt_secret or not rt_url:
        raise RuntimeError("NEON_REALTIME_URL and NEON_REALTIME_SECRET must be configured")

    has_node_sealer = (
        shutil.which("node") is not None
        and NEON_SEAL_SCRIPT.exists()
        and (ROOT_DIR / "node_modules" / "@neon" / "realtime").exists()
    )
    if not has_node_sealer:
        return _seal_neon_realtime_query_python(sql_query, params, rt_secret, get_database_name(), rt_url)

    payload = json.dumps({"sql": sql_query, "params": params or []})
    proc = subprocess.run(
        ["node", str(NEON_SEAL_SCRIPT), payload],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return _seal_neon_realtime_query_python(sql_query, params, rt_secret, get_database_name(), rt_url)

    return json.loads(proc.stdout.strip())
