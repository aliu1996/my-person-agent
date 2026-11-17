import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hash_password(password: str) -> str:
    """使用 scrypt 对密码做加盐哈希。"""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """校验 scrypt 密码哈希。"""
    try:
        salt_b64, dk_b64 = stored_hash.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _derive_aes_key(master_key: str) -> bytes:
    """将任意长度主密钥稳定派生为 32 字节 AES-256 密钥。"""
    return hashlib.sha256(master_key.encode("utf-8")).digest()


def encrypt_secret(plain: str, master_key: str) -> str:
    """使用 AES-256-GCM 加密敏感配置（如上游 API Key）。"""
    nonce = os.urandom(12)
    aes = AESGCM(_derive_aes_key(master_key))
    ct = aes.encrypt(nonce, plain.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_secret(token: str, master_key: str) -> str:
    """解密 AES-256-GCM 密文。"""
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    aes = AESGCM(_derive_aes_key(master_key))
    return aes.decrypt(nonce, ct, None).decode("utf-8")


def mask_secret(plain: str) -> str:
    """脱敏显示密钥，如 sk-****9a21。"""
    if len(plain) <= 8:
        return "****"
    return f"{plain[:3]}****{plain[-4:]}"


def create_jwt(subject: str, secret: str, role: str, expires_hours: int = 168) -> str:
    """签发用户端或管理端 JWT（通过不同密钥与 role 彻底隔离）。"""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_jwt(token: str, secret: str, expected_role: str) -> dict[str, Any]:
    """校验 JWT 并强制核对 role，防止 C 端 Token 越权访问管理接口。"""
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    if payload.get("role") != expected_role:
        raise jwt.InvalidTokenError("role mismatch")
    return payload
