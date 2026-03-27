import datetime
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes.runs import router as runs_router
from app.api.routes.account import router as account_router
from app.api.routes.approval import router as approval_router
from app.api.routes.approvals_queue import router as approvals_queue_router
from app.api.routes.audit import router as audit_router
from app.api.routes.chat import router as chat_router
from app.api.routes.institutions import router as institutions_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.org import router as org_router
from app.api.routes.team import router as team_router
from app.api.routes.transactions import router as transactions_router
from app.api.auth import auth_router
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


app = FastAPI(
    title="FlowPilot",
    description="Multi-agent fintech execution system powered by Interswitch APIs",
    version="0.1.0",
    lifespan=lifespan,
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
app.include_router(account_router, prefix="/api/v1", tags=["account"])
app.include_router(chat_router, prefix="/api/v1", tags=["chat"])
app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(approvals_queue_router, prefix="/api/v1", tags=["approvals-queue"])
app.include_router(audit_router, prefix="/api/v1", tags=["audit"])
app.include_router(institutions_router, prefix="/api/v1", tags=["institutions"])
app.include_router(notifications_router, prefix="/api/v1", tags=["notifications"])
app.include_router(onboarding_router, prefix="/api/v1", tags=["onboarding"])
app.include_router(org_router, prefix="/api/v1", tags=["org"])
app.include_router(team_router, prefix="/api/v1", tags=["team"])
app.include_router(transactions_router, prefix="/api/v1", tags=["transactions"])

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
