from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.wallet import router as wallet_router
from src.infrastructure.database.connection import close_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    try:
        yield
    finally:
        await close_db()


app = FastAPI(
    title="FlowPilot Wallet Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(wallet_router, prefix="/api/v1", tags=["wallet"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "wallet-service"}
