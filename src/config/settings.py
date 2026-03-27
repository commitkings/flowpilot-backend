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
            secret_arn = os.getenv("API_SECRETS_ARN")
            if secret_arn:
                logger.info("Initializing API secrets from Secrets Manager...")
                cls._api_secrets = _get_api_secrets()
                logger.info(f"Loaded {len(cls._api_secrets)} API secrets from Secrets Manager")
            else:
                logger.debug("API_SECRETS_ARN not set - using environment variables for secrets")

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
    INTERSWITCH_PAYOUTS_BASE_URL: str = os.getenv(
        "INTERSWITCH_PAYOUTS_BASE_URL", "https://api.interswitchng.com"
    )
    # Transaction Search (Quick / Reference Search) — see docs:
    # https://docs.interswitchgroup.com/docs/quick-search
    INTERSWITCH_TRANSACTION_SEARCH_BASE_URL: str = os.getenv(
        "INTERSWITCH_TRANSACTION_SEARCH_BASE_URL",
        "https://switch-online-gateway-service.k9.isw.la",
    )
    # Transaction Search passport (auth endpoint) — separate from main QA Passport
    # https://docs.interswitchgroup.com/docs/transactionapi-authentication
    INTERSWITCH_TRANSACTION_SEARCH_PASSPORT_URL: str = os.getenv(
        "INTERSWITCH_TRANSACTION_SEARCH_PASSPORT_URL",
        "https://passport-v2.k8.isw.la",
    )
    # Bank Account Verification (Marketplace routing)
    INTERSWITCH_BAV_BASE_URL: str = os.getenv(
        "INTERSWITCH_BAV_BASE_URL",
        "https://api-marketplace-routing.k8.isw.la",
    )
    INTERSWITCH_CLIENT_ID: Optional[str] = os.getenv("INTERSWITCH_CLIENT_ID")
    INTERSWITCH_CLIENT_SECRET: Optional[str] = os.getenv("INTERSWITCH_CLIENT_SECRET")
    INTERSWITCH_MERCHANT_ID: str = os.getenv("INTERSWITCH_MERCHANT_ID", "")
    INTERSWITCH_WALLET_ID: str = os.getenv("INTERSWITCH_WALLET_ID", "")
    INTERSWITCH_WALLET_PIN: str = os.getenv("INTERSWITCH_WALLET_PIN", "")
    # DEPRECATED: used only by legacy Quickteller endpoints
    INTERSWITCH_SOURCE_ACCOUNT_ID: str = os.getenv("INTERSWITCH_SOURCE_ACCOUNT_ID", "")
    INTERSWITCH_TERMINAL_ID: str = os.getenv("INTERSWITCH_TERMINAL_ID", "3PBL0001")

    _VALID_PAYOUT_MODES = ("simulated", "lookup_only", "live")
    PAYOUT_MODE: str = os.getenv("PAYOUT_MODE", "simulated")

    @classmethod
    def get_interswitch_access_token(cls) -> Optional[str]:
        return cls._get_secret("INTERSWITCH_ACCESS_TOKEN", "INTERSWITCH_ACCESS_TOKEN")

    @classmethod
    def is_interswitch_configured(cls) -> bool:
        has_oauth2 = bool(cls.INTERSWITCH_CLIENT_ID and cls.INTERSWITCH_CLIENT_SECRET)
        has_static_token = bool(cls.get_interswitch_access_token())
        return has_oauth2 or has_static_token

    @classmethod
    def is_payout_configured(cls) -> bool:
        """Check if wallet-based payout credentials are set."""
        return cls.is_interswitch_configured() and bool(cls.INTERSWITCH_WALLET_ID and cls.INTERSWITCH_WALLET_PIN)

    @classmethod
    def is_payout_simulated(cls) -> bool:
        return cls.PAYOUT_MODE.lower() in ("simulated", "lookup_only")

    @classmethod
    def is_reconciliation_simulated(cls) -> bool:
        """Only fully simulated mode should disable transaction reconciliation."""
        return cls.PAYOUT_MODE.lower() == "simulated"

    @classmethod
    def validate_payout_config(cls) -> list[str]:
        """Return a list of configuration warnings/errors for payout setup."""
        warnings: list[str] = []
        mode = cls.PAYOUT_MODE.lower()

        if mode not in cls._VALID_PAYOUT_MODES:
            warnings.append(
                f"PAYOUT_MODE={cls.PAYOUT_MODE!r} is invalid. "
                f"Must be one of {cls._VALID_PAYOUT_MODES}. Defaulting to 'simulated'."
            )

        if mode == "live":
            if not cls.is_payout_configured():
                warnings.append(
                    "PAYOUT_MODE=live but wallet credentials are missing "
                    "(INTERSWITCH_WALLET_ID / INTERSWITCH_WALLET_PIN / auth)."
                )
            base = cls.INTERSWITCH_BASE_URL.lower()
            payouts_base = cls.INTERSWITCH_PAYOUTS_BASE_URL.lower()
            base_is_qa = "qa.interswitchng" in base
            payouts_is_prod = "api.interswitchng" in payouts_base and "qa" not in payouts_base
            if base_is_qa and payouts_is_prod:
                warnings.append(
                    "Environment mismatch: INTERSWITCH_BASE_URL points to QA "
                    "but INTERSWITCH_PAYOUTS_BASE_URL points to production. "
                    "Auth tokens acquired from QA will be rejected by production."
                )

        if mode == "lookup_only":
            if not cls.is_interswitch_configured():
                warnings.append(
                    "PAYOUT_MODE=lookup_only but Interswitch auth credentials "
                    "are missing (INTERSWITCH_CLIENT_ID / INTERSWITCH_CLIENT_SECRET)."
                )

        return warnings

    # ------------------------------------------------------------------
    # Redis (short-term working memory mirror for chat)
    # ------------------------------------------------------------------
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL", "").strip() or None

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
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3001")
    PASSWORD_MIN_LENGTH: int = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))
    PASSWORD_HASH_ITERATIONS: int = int(
        os.getenv("PASSWORD_HASH_ITERATIONS", "600000")
    )
    PASSWORD_RESET_TOKEN_EXPIRY_MINUTES: int = int(
        os.getenv("PASSWORD_RESET_TOKEN_EXPIRY_MINUTES", "30")
    )
    PASSWORD_RESET_PATH: str = os.getenv("PASSWORD_RESET_PATH", "/reset-password")

    @classmethod
    def get_google_client_id(cls) -> Optional[str]:
        return cls._get_secret("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_ID")

    @classmethod
    def get_google_client_secret(cls) -> Optional[str]:
        return cls._get_secret("GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET")

    @classmethod
    def is_google_oauth_configured(cls) -> bool:
        return bool(cls.get_google_client_id() and cls.get_google_client_secret())

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
