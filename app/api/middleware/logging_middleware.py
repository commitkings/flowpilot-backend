"""
HTTP Request/Response Logging Middleware for FlowPilot.

Provides:
- Unique request ID generation for distributed tracing
- Request/response timing
- Structured logging of HTTP interactions
- Request context propagation via context vars
"""

import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.utilities.logging_config import (
    get_logger,
    log_request,
    request_id_var,
)

logger = get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs all HTTP requests and responses.

    Features:
    - Generates unique request ID (X-Request-ID header)
    - Logs request start with method, path, client IP
    - Logs response with status code and duration
    - Sets request_id context var for downstream logging
    """

    # Paths to skip logging (health checks, static files, etc.)
    SKIP_PATHS = {"/health", "/healthz", "/ready", "/metrics", "/favicon.ico"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip logging for certain paths
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        # Generate or extract request ID
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:12]
        
        # Set context var for downstream logging
        token = request_id_var.set(request_id)

        # Get client IP
        client_ip = request.client.host if request.client else None
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()

        # Log request start
        method = request.method
        path = request.url.path
        query = str(request.url.query) if request.url.query else ""
        full_path = f"{path}?{query}" if query else path

        log_request(method, full_path, client_ip=client_ip)

        # Process request and measure duration
        start_time = time.perf_counter()
        try:
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            # Log response
            log_request(method, full_path, response.status_code, duration_ms, client_ip)

            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as exc:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(
                f"{method} {full_path} -> ERROR after {duration_ms}ms: {type(exc).__name__}: {exc}"
            )
            raise

        finally:
            # Reset context var
            request_id_var.reset(token)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Lightweight middleware that only sets request context without logging.
    Use when LoggingMiddleware is too verbose.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:12]
        token = request_id_var.set(request_id)
        
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)
