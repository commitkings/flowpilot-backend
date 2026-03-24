import datetime
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routes.runs import router as runs_router
from app.api.routes.approval import router as approval_router
from app.api.routes.audit import router as audit_router
from app.api.routes.institutions import router as institutions_router
from src.infrastructure.database.connection import close_db, get_session_factory, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("FlowPilot API starting up")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(audit_router, prefix="/api/v1", tags=["audit"])
app.include_router(institutions_router, prefix="/api/v1", tags=["institutions"])


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
        "service": "flowpilot",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
