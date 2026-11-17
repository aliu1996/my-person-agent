import hashlib
import hmac
import secrets
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Annotated

import httpx
import jwt
from fastapi import APIRouter
from pa_shared import ErrorCode, get_settings, ok
from pa_shared.points import refresh_point_account
from pa_shared.security import encrypt_secret
from pydantic import Field, SecretStr
from sqlalchemy import text

from ..database import Database
from .common import (
    Cache,
    ClientError,
    ClientRoute,
    Input,
    active_user,
    unauthorized,
    unavailable,
    user_claims,
)

router = APIRouter(prefix="/auth", route_class=ClientRoute)
CHINA = timezone(timedelta(hours=8))
OTP_TTL = 300
OTP_RESERVE = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
local used = tonumber(redis.call('GET', KEYS[3]) or '0')
if used >= 10 then return 0 end
redis.call('SET', KEYS[2], '1', 'EX', 60)
if redis.call('INCR', KEYS[3]) == 1 then
    redis.call('EXPIRE', KEYS[3], ARGV[2])
end
redis.call('HSET', KEYS[1], 'value', ARGV[1], 'attempts', 0)
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""
OTP_VERIFY = """
local expected = redis.call('HGET', KEYS[1], 'value')
if not expected then return 0 end
if expected == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
end
if redis.call('HINCRBY', KEYS[1], 'attempts', 1) >= 5 then
    redis.call('DEL', KEYS[1])
end
return 0
"""
OTP_CANCEL = """
if redis.call('HGET', KEYS[1], 'value') == ARGV[1] then
    redis.call('DEL', KEYS[1])
end
"""


class PhoneRequest(Input):
    phone: Annotated[str, Field(pattern=r"^1[3-9][0-9]{9}$")]


class PhoneLogin(PhoneRequest):
    otpCode: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class Refresh(Input):
    refreshToken: Annotated[SecretStr, Field(min_length=1, max_length=4096)]


def private_hash(purpose: str, value: str) -> str:
    key = get_settings().phone_hash_secret.get_secret_value().encode()
    return hmac.new(key, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def otp_keys(phone_hash: str) -> tuple[str, str, str]:
    # Redis Cluster requires keys passed to the same script to share a hash slot.
    slot = "{" + phone_hash + "}"
    return f"auth:otp:{slot}", f"auth:rl:cd:{slot}", f"auth:rl:day:{slot}"


def issue_token(user_id: int, version: int, role: str, days: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "ver": version,
            "iat": now,
            "exp": now + timedelta(days=days),
            "jti": secrets.token_urlsafe(16),
        },
        get_settings().user_jwt_secret,
        algorithm="HS256",
    )


async def deliver_otp(phone: str, code: str) -> None:
    settings = get_settings()
    if settings.env == "dev":
        return
    url, token = settings.sms_webhook_url, settings.sms_webhook_token
    if not url or not url.startswith("https://") or not token:
        raise unavailable("SMS delivery is not configured")
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(
                url,
                json={"phone": phone, "code": code, "expiresIn": OTP_TTL},
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        raise unavailable("SMS delivery is temporarily unavailable") from None


@router.post("/sendOtp")
async def send_otp(body: PhoneRequest, cache: Cache):
    settings = get_settings()
    code = settings.otp_dev_code if settings.env == "dev" else f"{secrets.randbelow(10**6):06d}"
    phone_hash = private_hash("phone", body.phone)
    code_hash = private_hash("otp", f"{phone_hash}:{code}")
    keys = otp_keys(phone_hash)
    now = datetime.now(CHINA)
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, CHINA)
    seconds_left = max(1, int((midnight - now).total_seconds()) + 1)
    if not await cache.eval(OTP_RESERVE, len(keys), *keys, code_hash, seconds_left, OTP_TTL):
        raise ClientError("OTP request rate limit reached", ErrorCode.RATE_LIMITED, 429)
    try:
        await deliver_otp(body.phone, code)
    except ClientError:
        await cache.eval(OTP_CANCEL, 1, keys[0], code_hash)
        raise
    return ok({"expiresIn": OTP_TTL, "resendAfter": 60})


@router.post("/loginByPhone")
async def login_by_phone(body: PhoneLogin, cache: Cache, db: Database):
    phone_hash = private_hash("phone", body.phone)
    code_hash = private_hash("otp", f"{phone_hash}:{body.otpCode}")
    if not await cache.eval(OTP_VERIFY, 1, otp_keys(phone_hash)[0], code_hash):
        raise unauthorized()
    user = (
        (
            await db.execute(
                text("""
        INSERT INTO sys_user (phone_ciphertext, phone_hash)
        VALUES (:cipher, :phone_hash)
        ON CONFLICT (phone_hash) DO UPDATE SET phone_hash = excluded.phone_hash
        RETURNING id, status, token_version, delete_at > now() AS in_grace
    """),
                {
                    "cipher": encrypt_secret(body.phone, get_settings().key_encrypt_master),
                    "phone_hash": phone_hash,
                },
            )
        )
        .mappings()
        .one()
    )
    restored = user["status"] == "deleting" and bool(user["in_grace"])
    if not restored and user["status"] != "active":
        raise unauthorized()
    user_id, version = user["id"], user["token_version"]
    if restored:
        version = await db.scalar(
            text("""
            UPDATE sys_user SET status = 'active', delete_at = NULL,
                token_version = token_version + 1
            WHERE id = :id RETURNING token_version
        """),
            {"id": user_id},
        )
    balance = await refresh_point_account(db, user_id)
    await db.commit()
    return ok(
        {
            "userId": user_id,
            "userJwt": issue_token(user_id, version, "user", 7),
            "refreshToken": issue_token(user_id, version, "user_refresh", 30),
            "pointsBalance": balance,
            "accountRestored": restored,
        }
    )


@router.post("/refresh")
async def refresh_user_token(body: Refresh, db: Database):
    user_id, version = user_claims(body.refreshToken.get_secret_value(), "user_refresh")
    await active_user(db, user_id, version, write=False)
    return ok({"userJwt": issue_token(user_id, version, "user", 7)})
