"""MinIO / S3 storage client for FlowPilot document uploads.

Uses boto3 with the configured MINIO_ENDPOINT. Falls back gracefully if
the bucket is unreachable (e.g. local dev without a running MinIO instance).
"""

import logging
import mimetypes
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

_MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio.bureau.svc.cluster.local:9000")
_BUCKET = os.getenv("MINIO_BUCKET", "flowpilot")
_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "")
_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
_REGION = os.getenv("AWS_S3_REGION_NAME", "us-east-1")

_PRESIGNED_EXPIRY = 3600  # 1 hour for presigned URLs

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


def _get_client():
    """Return a boto3 S3 client pointed at MinIO."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=_MINIO_ENDPOINT,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name=_REGION,
        config=Config(signature_version="s3v4"),
    )


def _ensure_bucket(client) -> None:
    """Create the bucket if it doesn't exist."""
    try:
        client.head_bucket(Bucket=_BUCKET)
    except Exception:
        try:
            client.create_bucket(Bucket=_BUCKET)
            logger.info("Created MinIO bucket: %s", _BUCKET)
        except Exception as exc:
            logger.warning("Could not create bucket %s: %s", _BUCKET, exc)


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
    """
    if not content_type:
        guessed, _ = mimetypes.guess_type(filename)
        content_type = guessed or "application/octet-stream"

    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    object_key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    try:
        client = _get_client()
        _ensure_bucket(client)
        client.put_object(
            Bucket=_BUCKET,
            Key=object_key,
            Body=file_bytes,
            ContentType=content_type,
        )
        logger.info("Uploaded %s → s3://%s/%s", filename, _BUCKET, object_key)
        return object_key
    except Exception as exc:
        logger.error("MinIO upload failed for %s: %s", filename, exc)
        return None


def get_presigned_url(object_key: str, expiry: int = _PRESIGNED_EXPIRY) -> Optional[str]:
    """Generate a presigned GET URL for the given object key.

    Returns None if the object doesn't exist or MinIO is unreachable.
    """
    try:
        client = _get_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _BUCKET, "Key": object_key},
            ExpiresIn=expiry,
        )
        return url
    except Exception as exc:
        logger.error("Failed to generate presigned URL for %s: %s", object_key, exc)
        return None
