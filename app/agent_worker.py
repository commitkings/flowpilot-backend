import asyncio
import logging

from src.infrastructure.database.connection import close_db, init_db

logger = logging.getLogger(__name__)


async def _run() -> None:
    await init_db()
    logger.info("Agent worker service started")
    try:
        while True:
            await asyncio.sleep(60)
    finally:
        await close_db()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
