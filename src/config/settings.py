"""
Central Configuration for FlowPilot - Multi-Agent Fintech System.

All environment variables loaded here. Never hardcode secrets.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional
from functools import lru_cache
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(project_root / ".env")

logger = logging.getLogger(__name__)


def _fetch_secret_from_arn(secret_arn: str) -> Optional[dict]:
    """Fetch secret value from AWS Secrets Manager."""
    try:
        import boto3
        logger.info(f"Fetching secret from ARN: {secret_arn[:60]}...")
        client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
        response = client.get_secret_value(SecretId=secret_arn)
        secret_data = json.loads(response["SecretString"])
        logger.info(f"✅ Successfully loaded {len(secret_data)} keys from Secrets Manager")
        return secret_data
    except Exception as e:
        logger.error(f"Failed to fetch secret {secret_arn}: {type(e).__name__}: {e}")
        return None


def _get_database_url() -> Optional[str]:
    """Get DATABASE_URL from env or construct from Secrets Manager."""
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")

    secret_arn = os.getenv("DATABASE_URL_SECRET_ARN")
    if not secret_arn:
        return None

    secret = _fetch_secret_from_arn(secret_arn)
    if not secret:
        return None

    host = os.getenv("DB_HOST") or secret.get("host")
    port = secret.get("port", 5432)
    username = secret.get("username")
    password = secret.get("password")
    dbname = os.getenv("DB_NAME", "flowpilot")

    if not all([host, username, password]):
        logger.error("Missing required database credentials in secret")
        return None

    url = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
    logger.info(f"Constructed DATABASE_URL for host: {host}")
    return url


_cached_database_url: Optional[str] = None


def get_database_url() -> Optional[str]:
    global _cached_database_url
    if _cached_database_url is None:
        _cached_database_url = _get_database_url()
    return _cached_database_url

def _get_api_secrets() -> dict:
    secret_arn = os.getenv("API_SECRETS_ARN")
    if not secret_arn:
        return {}
    secret = _fetch_secret_from_arn(secret_arn)
    return secret or {}


class Settings:

    _api_secrets: dict = {}

    @classmethod
    def _init_api_secrets(cls) -> None:
        if not cls._api_secrets:
            logger.info("Initializing API secrets from Secrets Manager...")
            cls._api_secrets = _get_api_secrets()
            logger.info(f"Loaded {len(cls._api_secrets)} API secrets")

    @classmethod
    def _get_secret(cls, env_key: str, secret_key: str) -> Optional[str]:
        env_val = os.getenv(env_key)
        if env_val:
            return env_val
        cls._init_api_secrets()
        return cls._api_secrets.get(secret_key)

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------
    _database_url: Optional[str] = None

    @classmethod
    def get_database_url(cls) -> Optional[str]:
        if cls._database_url is None:
            cls._database_url = get_database_url()
        return cls._database_url

    @property
    def DATABASE_URL(self) -> Optional[str]:
        return self.get_database_url()

    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "10"))
    DATABASE_POOL_TIMEOUT: int = int(os.getenv("DATABASE_POOL_TIMEOUT", "30"))
    DATABASE_ECHO: bool = os.getenv("DATABASE_ECHO", "false").lower() == "true"

    @classmethod
    def is_database_configured(cls) -> bool:
        return bool(cls.get_database_url())

    @classmethod
    def get_async_database_url(cls) -> Optional[str]:
        url = cls.get_database_url()
        if not url:
            return None
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    # ------------------------------------------------------------------
    # Interswitch API
    # ------------------------------------------------------------------
    INTERSWITCH_BASE_URL: str = os.getenv("INTERSWITCH_BASE_URL", "https://qa.interswitchng.com")
    INTERSWITCH_CLIENT_ID: Optional[str] = os.getenv("INTERSWITCH_CLIENT_ID")
    INTERSWITCH_CLIENT_SECRET: Optional[str] = os.getenv("INTERSWITCH_CLIENT_SECRET")
    INTERSWITCH_MERCHANT_ID: str = os.getenv("INTERSWITCH_MERCHANT_ID", "")
    INTERSWITCH_SOURCE_ACCOUNT_ID: str = os.getenv("INTERSWITCH_SOURCE_ACCOUNT_ID", "")
    INTERSWITCH_TERMINAL_ID: str = os.getenv("INTERSWITCH_TERMINAL_ID", "3PBL0001")

    @classmethod
    def get_interswitch_access_token(cls) -> Optional[str]:
        return cls._get_secret("INTERSWITCH_ACCESS_TOKEN", "INTERSWITCH_ACCESS_TOKEN")

    @classmethod
    def is_interswitch_configured(cls) -> bool:
        has_oauth2 = bool(cls.INTERSWITCH_CLIENT_ID and cls.INTERSWITCH_CLIENT_SECRET)
        has_static_token = bool(cls.get_interswitch_access_token())
        return has_oauth2 or has_static_token

    # ------------------------------------------------------------------
    # Groq API (LLM)
    # ------------------------------------------------------------------
    GROQ_API_KEY: Optional[str] = os.getenv("GROQ_API_KEY")
    GROQ_LLM_MODEL: str = os.getenv("GROQ_LLM_MODEL")

    @property
    def groq_api_key(self) -> Optional[str]:
        return self._get_secret("GROQ_API_KEY", "GROQ_API_KEY")

    @classmethod
    def is_groq_configured(cls) -> bool:
        return bool(cls._get_secret("GROQ_API_KEY", "GROQ_API_KEY"))

    # ------------------------------------------------------------------
    # Google OAuth
    # ------------------------------------------------------------------
    GOOGLE_CLIENT_ID: Optional[str] = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET: Optional[str] = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI: str = os.getenv(
        "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/auth/google/callback"
    )
    JWT_SECRET: str = os.getenv("JWT_SECRET", "flowpilot-dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")

    @classmethod
    def is_google_oauth_configured(cls) -> bool:
        return bool(cls.GOOGLE_CLIENT_ID and cls.GOOGLE_CLIENT_SECRET)

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    @classmethod
    def is_production(cls) -> bool:
        return cls.ENVIRONMENT.lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
