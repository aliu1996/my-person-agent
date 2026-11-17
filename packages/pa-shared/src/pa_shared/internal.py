from hmac import compare_digest

from .settings import get_settings

INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def internal_headers() -> dict[str, str]:
    return {INTERNAL_TOKEN_HEADER: get_settings().internal_api_token.get_secret_value()}


def valid_internal_token(token: str) -> bool:
    settings = get_settings()
    expected = settings.internal_api_token.get_secret_value()
    if not expected or (settings.env != "dev" and len(expected) < 32):
        return False
    return compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
