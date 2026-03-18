"""Repository for RiskScoreFeature persistence and retrieval."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import RiskScoreFeatureModel


class RiskFeatureRepository:
    """Manages RiskScoreFeature persistence for per-candidate risk explainability."""

    MODEL_VERSION = "v2.0-weighted"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        candidate_id: UUID,
        run_id: UUID,
        features: dict,
    ) -> RiskScoreFeatureModel:
        """
        Create a risk feature record for a candidate.

        Args:
            candidate_id: The payout candidate ID
            run_id: The agent run ID
            features: Dict containing computed risk features:
                - historical_frequency: int (velocity_30d)
                - amount_deviation_ratio: Decimal (z_score)
                - avg_historical_amount: Decimal
                - duplicate_similarity_score: Decimal (0-1)
                - lookup_mismatch_flag: bool
                - account_anomaly_count: int
                - account_age_days: int
                - days_since_last_payout: int
                - amount_vs_budget_cap_pct: Decimal (0-1+)

        Returns:
            The created RiskScoreFeatureModel
        """
        model = RiskScoreFeatureModel(
            candidate_id=candidate_id,
            run_id=run_id,
            historical_frequency=features.get("historical_frequency"),
            amount_deviation_ratio=self._to_decimal(
                features.get("amount_deviation_ratio")
            ),
            avg_historical_amount=self._to_decimal(
                features.get("avg_historical_amount")
            ),
            duplicate_similarity_score=self._to_decimal(
                features.get("duplicate_similarity_score")
            ),
            lookup_mismatch_flag=features.get("lookup_mismatch_flag"),
            account_anomaly_count=features.get("account_anomaly_count"),
            account_age_days=features.get("account_age_days"),
            days_since_last_payout=features.get("days_since_last_payout"),
            amount_vs_budget_cap_pct=self._to_decimal(
                features.get("amount_vs_budget_cap_pct")
            ),
            model_version=self.MODEL_VERSION,
            computed_at=datetime.utcnow(),
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def get_by_candidate(
        self, candidate_id: UUID
    ) -> Optional[RiskScoreFeatureModel]:
        """Get risk features for a specific candidate."""
        stmt = select(RiskScoreFeatureModel).where(
            RiskScoreFeatureModel.candidate_id == candidate_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_run(self, run_id: UUID) -> list[RiskScoreFeatureModel]:
        """Get all risk features for a run."""
        stmt = select(RiskScoreFeatureModel).where(
            RiskScoreFeatureModel.run_id == run_id
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def batch_create(
        self,
        run_id: UUID,
        features_list: list[dict],
    ) -> list[RiskScoreFeatureModel]:
        """
        Create risk feature records for multiple candidates.

        Args:
            run_id: The agent run ID
            features_list: List of dicts, each containing:
                - candidate_id: UUID (required)
                - ...other feature fields

        Returns:
            List of created RiskScoreFeatureModel instances
        """
        models = []
        for features in features_list:
            candidate_id = features.get("candidate_id")
            if not candidate_id:
                continue

            model = RiskScoreFeatureModel(
                candidate_id=(
                    UUID(candidate_id)
                    if isinstance(candidate_id, str)
                    else candidate_id
                ),
                run_id=run_id,
                historical_frequency=features.get("historical_frequency"),
                amount_deviation_ratio=self._to_decimal(
                    features.get("amount_deviation_ratio")
                ),
                avg_historical_amount=self._to_decimal(
                    features.get("avg_historical_amount")
                ),
                duplicate_similarity_score=self._to_decimal(
                    features.get("duplicate_similarity_score")
                ),
                lookup_mismatch_flag=features.get("lookup_mismatch_flag"),
                account_anomaly_count=features.get("account_anomaly_count"),
                account_age_days=features.get("account_age_days"),
                days_since_last_payout=features.get("days_since_last_payout"),
                amount_vs_budget_cap_pct=self._to_decimal(
                    features.get("amount_vs_budget_cap_pct")
                ),
                model_version=self.MODEL_VERSION,
                computed_at=datetime.utcnow(),
            )
            models.append(model)

        if models:
            self._session.add_all(models)
            await self._session.flush()

        return models

    @staticmethod
    def _to_decimal(value) -> Optional[Decimal]:
        """Convert a value to Decimal, handling None and various types."""
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return None
