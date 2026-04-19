"""Redis-backed FIFO queue for agent run jobs.

Producer: create_run() in runs.py (LPUSH)
Consumer: agent_worker.py (BRPOP)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

QUEUE_KEY = "fp:agent:jobs"


async def enqueue_run(run_id: str, meta: dict) -> bool:
    """Push a run job onto the queue. Returns True on success."""
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return False
    try:
        import redis.asyncio as redis
        async with redis.from_url(url, decode_responses=True) as r:
            job = json.dumps({"run_id": run_id, **meta})
            await r.lpush(QUEUE_KEY, job)
            return True
    except Exception as exc:
        logger.warning("agent_queue: enqueue failed: %s", exc)
        return False


async def dequeue_run(r, timeout: int = 5) -> Optional[dict]:
    """BRPOP one job. Returns None on timeout. Takes a live Redis client."""
    try:
        result = await r.brpop(QUEUE_KEY, timeout=timeout)
        if result is None:
            return None
        _, raw = result
        return json.loads(raw)
    except Exception as exc:
        logger.warning("agent_queue: dequeue failed: %s", exc)
        return None
