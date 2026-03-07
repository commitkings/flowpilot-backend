import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routes.runs import router as runs_router
from app.api.routes.approval import router as approval_router
from app.api.routes.audit import router as audit_router
from app.api.routes.institutions import router as institutions_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.transactions import router as transactions_router
from app.api.auth import auth_router
from src.config.settings import Settings
from src.infrastructure.database.connection import close_db, get_session_factory, init_db

logger = logging.getLogger(__name__)


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

_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(audit_router, prefix="/api/v1", tags=["audit"])
app.include_router(institutions_router, prefix="/api/v1", tags=["institutions"])
app.include_router(onboarding_router, prefix="/api/v1", tags=["onboarding"])
app.include_router(transactions_router, prefix="/api/v1", tags=["transactions"])


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
