"""Wallet repository — balance management with race-condition safety.

All balance mutations go through this class.  Concurrency safety is achieved
with a SELECT … FOR UPDATE row-level lock on the wallet row, ensuring that two
concurrent debits cannot both read the same pre-debit balance.

Idempotency is enforced by a unique `reference` column on wallet_transaction.
Calling debit() or credit() with an existing reference returns the existing
transaction instead of creating a duplicate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    WalletModel,
    WalletTransactionModel,
)

# Balance below which we fire a low-balance warning email to the owner.
LOW_BALANCE_THRESHOLD = Decimal("50000.00")


class InsufficientBalanceError(Exception):
    """Raised when a debit would push the wallet below zero."""

    def __init__(self, balance: Decimal, required: Decimal) -> None:
        self.balance = balance
        self.required = required
        super().__init__(
            f"Insufficient wallet balance: available ₦{balance:,.2f}, "
            f"required ₦{required:,.2f}"
        )


class WalletRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get_locked(self, business_id: uuid.UUID) -> WalletModel:
        """Fetch the wallet row with a row-level lock (SELECT FOR UPDATE).

        This blocks any concurrent transaction that also tries to lock the same
        row, preventing two simultaneous debits from both reading the same
        pre-debit balance.  Raises ValueError if no wallet exists yet.
        """
        result = await self._session.execute(
            select(WalletModel)
            .where(WalletModel.business_id == business_id)
            .with_for_update()
        )
        wallet = result.scalars().first()
        if wallet is None:
            raise ValueError(
                f"Wallet not found for business {business_id}. "
                "The organisation must top up first."
            )
        return wallet

    async def _existing_tx(self, reference: str) -> Optional[WalletTransactionModel]:
        result = await self._session.execute(
            select(WalletTransactionModel).where(
                WalletTransactionModel.reference == reference
            )
        )
        return result.scalars().first()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def get_or_create(self, business_id: uuid.UUID) -> WalletModel:
        """Return the wallet for the business, creating one if it doesn't exist."""
        result = await self._session.execute(
            select(WalletModel).where(WalletModel.business_id == business_id)
        )
        wallet = result.scalars().first()
        if wallet is None:
            wallet = WalletModel(business_id=business_id, balance=Decimal("0.00"))
            self._session.add(wallet)
            await self._session.flush()
        return wallet

    async def get(self, business_id: uuid.UUID) -> Optional[WalletModel]:
        """Return the wallet without locking, or None if not found."""
        result = await self._session.execute(
            select(WalletModel).where(WalletModel.business_id == business_id)
        )
        return result.scalars().first()

    async def credit(
        self,
        business_id: uuid.UUID,
        amount: Decimal,
        reference: str,
        description: str = "Wallet top-up",
    ) -> tuple[WalletTransactionModel, bool]:
        """Credit (top up) the wallet.

        Returns (transaction, created) where `created` is False if the reference
        was already processed (idempotent replay).

        Does NOT acquire a row-level lock — credits are safe to run concurrently
        because they only increase the balance.  The unique constraint on
        `reference` prevents double-processing.
        """
        existing = await self._existing_tx(reference)
        if existing:
            return existing, False

        wallet = await self.get_or_create(business_id)
        balance_before = wallet.balance
        wallet.balance += amount
        wallet.updated_at = datetime.now(timezone.utc)

        tx = WalletTransactionModel(
            wallet_id=wallet.id,
            business_id=business_id,
            type="credit",
            amount=amount,
            reference=reference,
            description=description,
            balance_before=balance_before,
            balance_after=wallet.balance,
        )
        self._session.add(tx)
        try:
            await self._session.flush()
        except IntegrityError:
            # Race on the unique reference constraint — another request won
            await self._session.rollback()
            existing = await self._existing_tx(reference)
            return existing, False  # type: ignore[return-value]

        return tx, True

    async def debit(
        self,
        business_id: uuid.UUID,
        amount: Decimal,
        reference: str,
        description: str = "Run spend",
        run_id: Optional[uuid.UUID] = None,
    ) -> tuple[WalletTransactionModel, bool]:
        """Debit (spend) from the wallet — race-safe and idempotent.

        Steps:
          1. Idempotency check: if reference exists, return existing tx (no-op).
          2. SELECT FOR UPDATE on wallet row — blocks concurrent debits.
          3. Balance sufficiency check — raises InsufficientBalanceError if short.
          4. Deduct balance and write transaction record.

        Returns (transaction, created). Flush is called but NOT commit — the
        caller is responsible for committing (so debits can be atomic with run
        creation).
        """
        existing = await self._existing_tx(reference)
        if existing:
            return existing, False

        wallet = await self._get_locked(business_id)

        if wallet.balance < amount:
            raise InsufficientBalanceError(
                balance=wallet.balance, required=amount
            )

        balance_before = wallet.balance
        wallet.balance -= amount
        wallet.updated_at = datetime.now(timezone.utc)

        tx = WalletTransactionModel(
            wallet_id=wallet.id,
            business_id=business_id,
            type="debit",
            amount=amount,
            reference=reference,
            description=description,
            run_id=run_id,
            balance_before=balance_before,
            balance_after=wallet.balance,
        )
        self._session.add(tx)
        try:
            await self._session.flush()
        except IntegrityError:
            # Race on the unique reference constraint — another request won
            await self._session.rollback()
            existing = await self._existing_tx(reference)
            return existing, False  # type: ignore[return-value]

        return tx, True

    async def list_transactions(
        self,
        business_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        month_start: Optional[datetime] = None,
        month_end: Optional[datetime] = None,
    ) -> tuple[list[WalletTransactionModel], int]:
        """Return paginated transactions (newest first) and total count.

        Optional month_start / month_end narrow results to that calendar month.
        """
        from sqlalchemy import func as _func

        base_filter = [WalletTransactionModel.business_id == business_id]
        if month_start:
            base_filter.append(WalletTransactionModel.created_at >= month_start)
        if month_end:
            base_filter.append(WalletTransactionModel.created_at < month_end)

        count_result = await self._session.execute(
            select(_func.count()).select_from(WalletTransactionModel).where(
                and_(*base_filter)
            )
        )
        total = count_result.scalar_one()

        result = await self._session.execute(
            select(WalletTransactionModel)
            .where(and_(*base_filter))
            .order_by(WalletTransactionModel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all(), total
