"""
External services for Nigerian AI Study Assistant.
"""

from src.infrastructure.external_services.llm_service import llm_service
from src.infrastructure.external_services.embedding_service import embedding_service
from src.infrastructure.external_services.tts_service import tts_service
from src.infrastructure.external_services.vector_db_service import vector_db_service

__all__ = [
    "llm_service",
    "embedding_service",
    "tts_service",
    "vector_db_service"
]
