"""MinIO / S3 storage client for FlowPilot document uploads.

Uses boto3 with the configured MINIO_ENDPOINT. Falls back gracefully if
the bucket is unreachable (e.g. local dev without a running MinIO instance).

Performance notes:
- The boto3 client is created once and reused (module-level singleton).
- The bucket is checked for existence only once per process lifetime.
- All blocking boto3 I/O runs in a thread pool so the asyncio event loop
  is never blocked during uploads or presigned-URL generation.
"""

import asyncio
import logging
import mimetypes
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio.bureau.svc.cluster.local:9000")
# Public-facing endpoint used for generating presigned URLs that browsers can reach.
# Defaults to the internal endpoint (works for local dev where MinIO isn't used).
_MINIO_PUBLIC_ENDPOINT = os.getenv("MINIO_PUBLIC_ENDPOINT", _MINIO_ENDPOINT)
_BUCKET = os.getenv("MINIO_BUCKET", "flowpilot")
_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "")
_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
_REGION = os.getenv("AWS_S3_REGION_NAME", "us-east-1")

_PRESIGNED_EXPIRY = 3600  # 1 hour for presigned URLs

# Thread pool for blocking boto3 I/O (keeps the async event loop free).
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="s3_upload")

# ── Magic-byte signatures for allowed file types ───────────────────────────────

_IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),            # JPEG
    (b"\x89PNG\r\n\x1a\n", "image/png"),        # PNG
    (b"GIF87a", "image/gif"),                   # GIF 87a
    (b"GIF89a", "image/gif"),                   # GIF 89a
    (b"RIFF", "image/webp"),                    # WebP (also needs bytes[8:12] == b'WEBP')
]

_DOC_SIGNATURES = [
    (b"%PDF-", "application/pdf"),
] + _IMAGE_SIGNATURES  # Documents may also be images (e.g. scanned IDs)


def _detect_mime(data: bytes) -> str | None:
    """Return the actual MIME type from magic bytes, or None if unrecognized."""
    for sig, mime in _IMAGE_SIGNATURES:
        if data[:len(sig)] == sig:
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


def validate_image(data: bytes, max_bytes: int = 3 * 1024 * 1024) -> str:
    """Validate image upload. Returns error string or '' if valid."""
    if len(data) > max_bytes:
        return f"Image exceeds {max_bytes // (1024*1024)} MB limit"
    mime = _detect_mime(data)
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return "Only JPEG, PNG, GIF, or WebP images are accepted"
    return ""


def validate_document(data: bytes, max_bytes: int = 10 * 1024 * 1024) -> str:
    """Validate document upload. Returns error string or '' if valid."""
    if len(data) > max_bytes:
        return f"File exceeds {max_bytes // (1024*1024)} MB limit"
    mime = _detect_mime(data)
    if mime is None:
        return "Unsupported file type. Only PDF, JPEG, or PNG documents are accepted"
    return ""


# ── Singleton boto3 clients ────────────────────────────────────────────────────

_upload_client = None   # client used for put_object (internal endpoint)
_public_client = None   # client used for presigned URLs (public endpoint)
_bucket_ready = False   # bucket existence checked at most once


def _get_upload_client():
    """Return (and lazily create) the singleton upload client."""
    global _upload_client
    if _upload_client is None:
        import boto3
        from botocore.config import Config
        _upload_client = boto3.client(
            "s3",
            endpoint_url=_MINIO_ENDPOINT,
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
            region_name=_REGION,
            config=Config(signature_version="s3v4"),
        )
    return _upload_client


def _get_public_client():
    """Return (and lazily create) the singleton presigned-URL client.

    Uses the public endpoint so generated URLs are reachable from browsers.
    """
    global _public_client
    if _public_client is None:
        import boto3
        from botocore.config import Config
        _public_client = boto3.client(
            "s3",
            endpoint_url=_MINIO_PUBLIC_ENDPOINT,
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
            region_name=_REGION,
            config=Config(signature_version="s3v4"),
        )
    return _public_client


def _ensure_bucket_sync(client) -> None:
    """Create the bucket if it doesn't exist. Only runs once per process."""
    global _bucket_ready
    if _bucket_ready:
        return
    try:
        client.head_bucket(Bucket=_BUCKET)
        _bucket_ready = True
    except Exception:
        try:
            client.create_bucket(Bucket=_BUCKET)
            _bucket_ready = True
            logger.info("Created MinIO bucket: %s", _BUCKET)
        except Exception as exc:
            logger.warning("Could not create bucket %s: %s", _BUCKET, exc)


def _upload_sync(file_bytes: bytes, object_key: str, content_type: str) -> bool:
    """Blocking upload — runs inside the thread pool."""
    client = _get_upload_client()
    _ensure_bucket_sync(client)
    client.put_object(
        Bucket=_BUCKET,
        Key=object_key,
        Body=file_bytes,
        ContentType=content_type,
    )
    return True


def _presigned_sync(object_key: str, expiry: int) -> Optional[str]:
    """Blocking presigned URL generation — runs inside the thread pool."""
    client = _get_public_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _BUCKET, "Key": object_key},
        ExpiresIn=expiry,
    )


async def upload_file(
    file_bytes: bytes,
    filename: str,
    folder: str = "kyc",
    content_type: Optional[str] = None,
) -> Optional[str]:
    """Upload bytes to MinIO and return the object key (path in bucket).

    Returns the object key on success, None on failure.
    The caller should store the key and use get_presigned_url() to generate
    time-limited download URLs.

    All blocking I/O runs in a thread pool so the asyncio event loop is free.
    """
    if not content_type:
        guessed, _ = mimetypes.guess_type(filename)
        content_type = guessed or "application/octet-stream"

    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    object_key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_executor, _upload_sync, file_bytes, object_key, content_type)
        logger.info("Uploaded %s → s3://%s/%s", filename, _BUCKET, object_key)
        return object_key
    except Exception as exc:
        logger.error("MinIO upload failed for %s: %s", filename, exc)
        return None


def get_presigned_url(object_key: str, expiry: int = _PRESIGNED_EXPIRY) -> Optional[str]:
    """Generate a presigned GET URL for the given object key.

    Returns None if the object doesn't exist or MinIO is unreachable.
    Note: this is a synchronous function — call from a thread if needed in async context.
    """
    try:
        return _presigned_sync(object_key, expiry)
    except Exception as exc:
        logger.error("Failed to generate presigned URL for %s: %s", object_key, exc)
        return None


async def download_file(object_key: str) -> Optional[bytes]:
    """Download a file from MinIO and return its bytes. Returns None on failure."""
    try:
        loop = asyncio.get_running_loop()
        def _download_sync():
            client = _get_upload_client()
            resp = client.get_object(Bucket=_BUCKET, Key=object_key)
            return resp["Body"].read()
        return await loop.run_in_executor(_executor, _download_sync)
    except Exception as exc:
        logger.error("Failed to download %s from MinIO: %s", object_key, exc)
        return None


def make_file_url(object_key: Optional[str]) -> Optional[str]:
    """Construct a browser-accessible URL for a MinIO object via the backend file proxy.

    Uses API_BASE_URL env var (e.g. https://api.flowpilot.club).
    Falls back to a local /uploads path for files stored locally.
    """
    if not object_key:
        return None
    # Already a full URL (legacy presigned URL or external URL) - try to extract key
    if object_key.startswith("http://") or object_key.startswith("https://"):
        # Try to extract object key from MinIO URL: http://endpoint/bucket/key?sig
        try:
            from urllib.parse import urlparse
            parsed = urlparse(object_key)
            path = parsed.path.lstrip("/")
            if path.startswith(_BUCKET + "/"):
                key = path[len(_BUCKET) + 1:]
            else:
                key = path
            # Remove any query params - we have the key, reconstruct URL
            if key:
                object_key = key
            else:
                return object_key  # can't extract key, return as-is
        except Exception:
            return object_key
    # Local upload path - return as-is (served via StaticFiles or Next.js proxy)
    if object_key.startswith("/uploads/") or object_key.startswith("uploads/"):
        return object_key
    # Construct backend proxy URL
    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    if api_base:
        return f"{api_base}/api/v1/files/{object_key}"
    # No API_BASE_URL set - fall back to relative path (works if same origin)
    return f"/api/v1/files/{object_key}"


def make_url_public(stored_url: Optional[str], expiry: int = 30 * 24 * 3600) -> Optional[str]:
    """Rewrite a stored avatar/logo URL to be browser-accessible.

    Handles three cases:
    - None / empty  → returned as-is.
    - Local path (/uploads/...) → returned as-is (Next.js proxies these).
    - MinIO internal URL → extracts the object_key and regenerates the presigned
      URL using the public endpoint (MINIO_PUBLIC_ENDPOINT env var).
      If MINIO_PUBLIC_ENDPOINT equals the internal endpoint (not configured),
      the original URL is returned unchanged.
    """
    if not stored_url:
        return stored_url
    # Local file — served via Next.js /uploads rewrite.
    if stored_url.startswith("/uploads/") or stored_url.startswith("uploads/"):
        return stored_url
    # Not a MinIO URL at all (e.g. already an http URL we don't own).
    if _MINIO_ENDPOINT not in stored_url and _MINIO_PUBLIC_ENDPOINT not in stored_url:
        return stored_url
    # If no distinct public endpoint is configured, we can't rewrite the URL.
    if _MINIO_PUBLIC_ENDPOINT == _MINIO_ENDPOINT:
        logger.warning(
            "MINIO_PUBLIC_ENDPOINT is not configured — avatar URLs will use the "
            "internal MinIO hostname and may not be reachable from browsers. "
            "Set MINIO_PUBLIC_ENDPOINT to the public-facing MinIO URL."
        )
        return stored_url
    # Extract object_key from path: http://endpoint:port/{bucket}/{object_key}?sig...
    try:
        from urllib.parse import urlparse
        parsed = urlparse(stored_url)
        path = parsed.path.lstrip("/")
        if path.startswith(_BUCKET + "/"):
            object_key = path[len(_BUCKET) + 1:]
        else:
            object_key = path
        regenerated = get_presigned_url(object_key, expiry=expiry)
        return regenerated or stored_url
    except Exception as exc:
        logger.error("Failed to rewrite MinIO URL %s: %s", stored_url, exc)
        return stored_url
