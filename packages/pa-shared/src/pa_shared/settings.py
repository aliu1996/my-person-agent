from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局环境变量配置，统一从 .env 或容器环境变量读取。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"

    database_url: str = "postgresql+asyncpg://pa:pa_dev@127.0.0.1:5432/personal_assistant"
    redis_url: str = "redis://127.0.0.1:6379/0"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "pa-assets"
    s3_public_endpoint: str | None = None
    s3_region: str = "us-east-1"

    higress_gateway_url: str = "http://127.0.0.1:8080"
    higress_console_url: str = "http://127.0.0.1:8001"
    higress_console_username: str | None = None
    higress_console_password: SecretStr | None = None
    higress_gateway_key: SecretStr | None = None
    higress_gateway_consumer: str | None = None
    chat_api_url: str = "http://127.0.0.1:8100"
    chat_stream_url: str = "http://127.0.0.1:8101"
    search_service_url: str = "http://127.0.0.1:8102"
    dsh_bridge_url: str = "http://127.0.0.1:8103"
    dsh_bridge_urls: str = ""
    otel_exporter_otlp_traces_endpoint: str | None = None
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    internal_api_token: SecretStr = SecretStr("dev-internal-token-change-me")
    bocha_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None

    user_jwt_secret: str = "dev-user-secret-change-me-at-least-32b"
    phone_hash_secret: SecretStr = SecretStr("dev-phone-hmac-secret-change-me")
    otp_dev_code: str = Field(default="123456", pattern=r"^[0-9]{6}$")
    sms_webhook_url: str | None = None
    sms_webhook_token: SecretStr | None = None
    share_report_threshold: int = Field(default=3, ge=1)
    admin_jwt_secret: str = "dev-admin-secret-change-me-at-least-32b"
    key_encrypt_master: str = "dev-32bytes-aes-key-placeholder1"
    init_admin_username: str = "admin"
    init_admin_password: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
