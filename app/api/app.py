import datetime
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes.account import router as account_router
from app.api.routes.chat import router as chat_router
from app.api.routes.institutions import router as institutions_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.org import router as org_router
from app.api.routes.team import router as team_router
from app.api.routes.transactions import router as transactions_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.developer import router as developer_router
from app.api.routes.org_config import router as org_config_router
from app.api.routes.public_api import router as public_api_router
from app.api.routes.files import router as files_router
from app.api.routes.recipients import router as recipients_router
from app.api.routes.payee import router as payee_router
from app.api.routes.monnify_webhooks import router as monnify_webhooks_router
from app.api.auth import auth_router, two_factor_router
from app.api.auth.approval_pin import router as approval_pin_router
from app.api.middleware import LoggingMiddleware
from src.config.settings import Settings
from src.infrastructure.database.connection import (
    close_db,
    get_session_factory,
    init_db,
)
from src.utilities.logging_config import get_logger, setup_logging

# Initialize logging system before anything else
setup_logging()
logger = get_logger(__name__)


def _validate_payout_config() -> None:
    """Log payout mode and surface any configuration warnings at startup."""
    mode = Settings.PAYOUT_MODE.lower()
    logger.info(f"PAYOUT_MODE={mode}")

    warnings = Settings.validate_payout_config()
    for warning in warnings:
        logger.warning(f"[PayoutConfig] {warning}")

    if mode == "simulated":
        logger.info(
            "Payout transport is SIMULATED — no real funds will move. "
            "Set PAYOUT_MODE=live with valid credentials to enable real payouts."
        )

    if Settings.is_production() and Settings.has_weak_jwt_secret():
        raise RuntimeError(
            "Weak JWT_SECRET detected in production. "
            "Set a strong secret (>=32 chars) and restart."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("FlowPilot API starting up")
    _validate_payout_config()
    await init_db()
    try:
        yield
    finally:
        await close_db()
        logger.info("FlowPilot API shutting down")


_is_prod = Settings.is_production()
app = FastAPI(
    title="FlowPilot",
    description="Multi-agent fintech execution system powered by Monnify APIs",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

_cors_origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3001,http://127.0.0.1:3001",
    ).split(",")
]

# Add logging middleware first (outermost - processes requests first)
app.add_middleware(LoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global exception handler for unhandled errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all exception handler that logs unhandled errors."""
    logger.exception(
        f"Unhandled exception on {request.method} {request.url.path}: "
        f"{type(exc).__name__}: {exc}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error_type": type(exc).__name__,
        },
    )

app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
app.include_router(two_factor_router, prefix="/api/v1/auth", tags=["2fa"])
app.include_router(approval_pin_router, prefix="/api/v1", tags=["approval-pin"])
app.include_router(account_router, prefix="/api/v1", tags=["account"])
app.include_router(chat_router, prefix="/api/v1", tags=["chat"])
app.include_router(institutions_router, prefix="/api/v1", tags=["institutions"])
app.include_router(notifications_router, prefix="/api/v1", tags=["notifications"])
app.include_router(org_router, prefix="/api/v1", tags=["org"])
app.include_router(team_router, prefix="/api/v1", tags=["team"])
app.include_router(transactions_router, prefix="/api/v1", tags=["transactions"])
app.include_router(dashboard_router, prefix="/api/v1", tags=["dashboard"])
app.include_router(developer_router, prefix="/api/v1", tags=["developer"])
app.include_router(org_config_router, prefix="/api/v1", tags=["org-config"])
app.include_router(public_api_router, prefix="/api/v1", tags=["public-api"])
app.include_router(files_router, prefix="/api/v1", tags=["files"])
app.include_router(recipients_router, prefix="/api/v1", tags=["recipients"])
app.include_router(payee_router, prefix="/api/v1", tags=["payee"])
app.include_router(monnify_webhooks_router, prefix="/api/v1", tags=["monnify-webhooks"])

# Serve uploaded files (avatars, etc.)
from fastapi.staticfiles import StaticFiles

_uploads_dir = os.path.join(os.getcwd(), "uploads")
os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")


@app.get("/health")
async def health_check() -> dict[str, str]:
    db_status = "healthy"
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_status = "unhealthy"
    return {
        "status": "healthy" if db_status == "healthy" else "unhealthy",
        "database": db_status,
        "payout_mode": Settings.PAYOUT_MODE.lower(),
        "service": "flowpilot",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
