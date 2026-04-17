"""
Factory for PayoutGateway — selects live or simulated based on PAYOUT_MODE.
"""

import logging

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway

logger = logging.getLogger(__name__)

_cached_gateway: PayoutGateway | None = None


def get_payout_gateway() -> PayoutGateway:
    """Return the singleton PayoutGateway matching the configured PAYOUT_MODE."""
    global _cached_gateway
    if _cached_gateway is not None:
        return _cached_gateway

    mode = Settings.PAYOUT_MODE.lower()

    provider = Settings.PAYOUT_PROVIDER

    if mode == "live" and provider == "monnify":
        from src.infrastructure.external_services.monnify.gateway import (
            MonnifyPayoutGateway,
        )

        _cached_gateway = MonnifyPayoutGateway()
        logger.info("PayoutGateway: LIVE mode via Monnify")
    elif mode == "live":
        from src.infrastructure.external_services.interswitch.live_gateway import (
            LivePayoutGateway,
        )
        _cached_gateway = LivePayoutGateway()
        logger.warning("PayoutGateway: LIVE mode via legacy Interswitch")
    elif mode == "lookup_only":
        from src.infrastructure.external_services.interswitch.hybrid_gateway import (
            HybridPayoutGateway,
        )
        _cached_gateway = HybridPayoutGateway()
        logger.info(
            "PayoutGateway: LOOKUP_ONLY mode — real account verification, simulated payouts"
        )
    else:
        from src.infrastructure.external_services.interswitch.simulated_gateway import (
            SimulatedPayoutGateway,
        )
        _cached_gateway = SimulatedPayoutGateway()
        logger.info("PayoutGateway: SIMULATED mode — no real funds will move")

    return _cached_gateway


def reset_gateway() -> None:
    """Clear the cached gateway (useful for tests)."""
    global _cached_gateway
    _cached_gateway = None
