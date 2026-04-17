from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.internal_payment import router as internal_payment_router
from app.api.routes.monnify_webhooks import router as monnify_webhooks_router
from src.infrastructure.database.connection import close_db, init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    try:
        yield
    finally:
        await close_db()


app = FastAPI(
    title="FlowPilot Payment Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(monnify_webhooks_router, prefix="/api/v1")
app.include_router(internal_payment_router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "payment-service", "provider": "monnify"}
