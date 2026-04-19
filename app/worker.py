import asyncio
import logging

from src.infrastructure.database.connection import close_db, init_db
from src.services.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


async def _run() -> None:
    await init_db()
    start_scheduler()
    logger.info("Worker service started")
    try:
        while True:
            await asyncio.sleep(60)
    finally:
        stop_scheduler()
        await close_db()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
