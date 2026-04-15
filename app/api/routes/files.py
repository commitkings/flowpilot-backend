"""File serving proxy — reads from MinIO and streams to browser.

GET /api/v1/files/{path:path}
"""
import mimetypes
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from src.infrastructure.storage import s3_client

router = APIRouter()


@router.get("/files/{path:path}")
async def serve_file(path: str):
    """Proxy a file from MinIO storage to the browser.

    This allows browsers to load avatars and logos even when MinIO
    is only accessible internally (no public hostname configured).
    """
    # Basic path safety — prevent directory traversal
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    content = await s3_client.download_file(path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "application/octet-stream"

    return Response(
        content=content,
        media_type=mime_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
