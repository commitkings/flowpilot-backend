import logging

from fastapi import APIRouter, HTTPException

from src.infrastructure.external_services.interswitch.payouts import PayoutClient

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/institutions")
async def list_institutions():
    try:
        client = PayoutClient()
        result = await client.get_receiving_institutions()
        return result
    except Exception as e:
        logger.error(f"Failed to fetch institutions: {e}")
        raise HTTPException(status_code=502, detail=f"Interswitch API error: {str(e)}")
