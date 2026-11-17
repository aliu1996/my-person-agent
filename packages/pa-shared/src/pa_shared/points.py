"""PostgreSQL point-account operations. The caller owns the transaction."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Interval,
    Text,
    Uuid,
    column,
    func,
    literal,
    literal_column,
    or_,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

OVERDRAFT_LIMIT = 20
account = table(
    "point_account",
    column("user_id", BigInteger),
    column("total_balance", BigInteger),
    column("debt", Integer),
    column("updated_at", DateTime(timezone=True)),
)
bucket = table(
    "point_bucket",
    column("id", BigInteger),
    column("user_id", BigInteger),
    column("bucket_type", Text),
    column("balance", Integer),
    column("priority", Integer),
    column("expire_at", DateTime(timezone=True)),
)
rule = table("point_rule", column("rule_key", Text), column("rule_value", Integer))
ledger = table(
    "point_ledger",
    column("id", BigInteger),
    column("user_id", BigInteger),
    column("turn_id", Uuid),
    column("delta", BigInteger),
    column("balance_after", BigInteger),
    column("detail_json", JSONB),
)


@dataclass(frozen=True)
class TurnSettlement:
    charged: int
    balance_after: int
    replayed: bool = False


async def refresh_point_account(db: AsyncSession, user_id: int) -> int:
    """Lock the account and reconcile expired buckets before reading or charging."""
    owner = account.c.user_id == user_id
    buckets = bucket.c.user_id == user_id
    daily = literal_column("'daily_free'", Text)
    shanghai = literal_column("'Asia/Shanghai'", Text)
    local_midnight = func.date_trunc("day", func.timezone(shanghai, func.now()))
    expires = func.timezone(shanghai, local_midnight + literal(timedelta(days=1), Interval()))
    await db.execute(
        insert(account).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"])
    )
    await db.execute(select(account.c.user_id).where(owner).with_for_update())
    daily_credit = insert(bucket).from_select(
        ["user_id", "bucket_type", "priority", "balance", "expire_at"],
        select(literal(user_id, BigInteger), daily, literal(1), rule.c.rule_value, expires).where(
            rule.c.rule_key == "daily_free_amount"
        ),
    )
    await db.execute(
        daily_credit.on_conflict_do_update(
            index_elements=[bucket.c.user_id],
            index_where=bucket.c.bucket_type == daily,
            set_={
                "balance": daily_credit.excluded.balance,
                "expire_at": daily_credit.excluded.expire_at,
            },
            where=or_(bucket.c.expire_at.is_(None), bucket.c.expire_at <= func.now()),
        )
    )
    await db.execute(
        bucket.update()
        .where(
            buckets,
            bucket.c.bucket_type != daily,
            bucket.c.expire_at <= func.now(),
            bucket.c.balance != 0,
        )
        .values(balance=0)
    )
    total = select(func.coalesce(func.sum(bucket.c.balance), 0)).where(buckets).scalar_subquery()
    return await db.scalar(
        account.update()
        .where(owner)
        .values(
            total_balance=total - account.c.debt,
            updated_at=func.now(),
        )
        .returning(account.c.total_balance)
    )


async def check_points_threshold(db: AsyncSession, user_id: int, threshold: int = 1) -> bool:
    return await refresh_point_account(db, user_id) >= threshold


async def settle_turn_points(
    db: AsyncSession,
    user_id: int,
    turn_id: UUID,
    cost: int,
    detail: dict,
) -> TurnSettlement:
    if type(cost) is not int or cost < 0:
        raise ValueError("Point cost must be a nonnegative integer")
    balance = await refresh_point_account(db, user_id)
    charged = min(cost, max(0, balance + OVERDRAFT_LIMIT))
    remaining = balance - charged
    inserted = await db.scalar(
        insert(ledger)
        .values(
            user_id=user_id,
            turn_id=turn_id,
            delta=-charged,
            balance_after=remaining,
            detail_json=detail,
        )
        .on_conflict_do_nothing(index_elements=[ledger.c.turn_id])
        .returning(ledger.c.id)
    )
    if inserted is None:
        entry = (
            await db.execute(
                select(ledger.c.user_id, ledger.c.delta, ledger.c.balance_after).where(
                    ledger.c.turn_id == turn_id
                )
            )
        ).one()
        if entry.user_id != user_id:
            raise ValueError("Turn belongs to a different user")
        return TurnSettlement(-entry.delta, entry.balance_after, replayed=True)

    owner = account.c.user_id == user_id
    debt = await db.scalar(select(account.c.debt).where(owner))
    pending = charged + debt
    if pending:
        buckets = (
            await db.execute(
                select(bucket.c.id, bucket.c.balance)
                .where(bucket.c.user_id == user_id, bucket.c.balance > 0)
                .order_by(bucket.c.priority, bucket.c.expire_at.asc().nulls_last(), bucket.c.id)
                .with_for_update()
            )
        ).all()
        for entry in buckets:
            debit = min(pending, entry.balance)
            await db.execute(
                bucket.update()
                .where(bucket.c.id == entry.id)
                .values(
                    balance=bucket.c.balance - debit,
                )
            )
            pending -= debit
            if pending == 0:
                break
    await db.execute(
        account.update()
        .where(owner)
        .values(
            total_balance=remaining,
            debt=pending,
            updated_at=func.now(),
        )
    )
    return TurnSettlement(charged, remaining)
