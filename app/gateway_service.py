import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware


SERVICE_URLS = {
    "core": os.getenv("CORE_API_URL", "http://core-api:8500"),
    "payment": os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8100"),
    "wallet": os.getenv("WALLET_SERVICE_URL", "http://wallet-service:8200"),
    "kyc": os.getenv("KYC_SERVICE_URL", "http://kyc-service:8300"),
    "orchestration": os.getenv(
        "ORCHESTRATION_SERVICE_URL", "http://orchestration-service:8400"
    ),
}

ROUTE_TABLE = [
    ("/api/v1/webhooks/monnify", "payment"),
    ("/api/v1/wallet", "wallet"),
    ("/api/v1/kyc", "kyc"),
    ("/api/v1/onboarding", "kyc"),
    ("/api/v1/runs", "orchestration"),
    ("/api/v1/approval", "orchestration"),
    ("/api/v1/approvals-queue", "orchestration"),
    ("/api/v1/audit", "orchestration"),
    ("/api/v1/scheduled-runs", "orchestration"),
]

EXCLUDED_HEADERS = {"host", "content-length", "connection"}


def _target_service(path: str) -> str:
    for prefix, service in ROUTE_TABLE:
        if path == prefix or path.startswith(f"{prefix}/"):
            return service
    return "core"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.http = httpx.AsyncClient(timeout=60.0)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="FlowPilot Gateway", version="0.1.0", lifespan=lifespan)

_cors_origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3001,http://127.0.0.1:3001",
    ).split(",")
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _forward(request: Request, path: str) -> Response:
    service = _target_service(path)
    base_url = SERVICE_URLS[service]
    target_url = f"{base_url}{path}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in EXCLUDED_HEADERS
    }
    body = await request.body()
    resp = await request.app.state.http.request(
        method=request.method,
        url=target_url,
        params=request.query_params,
        headers=headers,
        content=body,
    )
    passthrough_headers = {
        key: value
        for key, value in resp.headers.items()
        if key.lower() not in EXCLUDED_HEADERS
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=passthrough_headers,
        media_type=resp.headers.get("content-type"),
    )


@app.get("/health")
async def gateway_health() -> dict:
    return {
        "status": "healthy",
        "service": "gateway",
        "routes": {k: v for k, v in SERVICE_URLS.items()},
    }


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def api_gateway_proxy(path: str, request: Request) -> Response:
    return await _forward(request, f"/api/v1/{path}")


@app.api_route("/uploads/{path:path}", methods=["GET"])
async def uploads_gateway_proxy(path: str, request: Request) -> Response:
    return await _forward(request, f"/uploads/{path}")
