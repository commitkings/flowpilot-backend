from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.approval import router as approval_router
from app.api.routes.approvals_queue import router as approvals_queue_router
from app.api.routes.audit import router as audit_router
from app.api.routes.runs import router as runs_router
from app.api.routes.scheduled_runs import router as scheduled_runs_router
from src.infrastructure.database.connection import close_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    try:
        yield
    finally:
        await close_db()


app = FastAPI(
    title="FlowPilot Orchestration Service",
    version="0.1.0",
    lifespan=lifespan,
)

# IMPORTANT: include scheduled routes before /runs/{run_id}.
app.include_router(scheduled_runs_router, prefix="/api/v1", tags=["scheduled-runs"])
app.include_router(runs_router, prefix="/api/v1", tags=["runs"])
app.include_router(approval_router, prefix="/api/v1", tags=["approval"])
app.include_router(approvals_queue_router, prefix="/api/v1", tags=["approvals-queue"])
app.include_router(audit_router, prefix="/api/v1", tags=["audit"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "orchestration-service"}
