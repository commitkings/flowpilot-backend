from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.kyc import router as kyc_router
from app.api.routes.onboarding import router as onboarding_router
from src.infrastructure.database.connection import close_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    try:
        yield
    finally:
        await close_db()


app = FastAPI(
    title="FlowPilot KYC Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(onboarding_router, prefix="/api/v1", tags=["onboarding"])
app.include_router(kyc_router, prefix="/api/v1", tags=["kyc"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "kyc-service"}
