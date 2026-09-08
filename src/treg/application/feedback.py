"""Accept a report in one transaction, independently of best-effort call auditing."""

from sqlmodel import select

from .. import ratestore
from ..domain import feedback
from ..feedback_contract import FeedbackCategory
from ..infra.db import session_maker
from ..models import CallRecord, LedgerEntry

RATE_MAX = 30
RATE_WINDOW_S = 3600
RATE_NAMESPACE = "feedback"


class FeedbackRateLimited(Exception):
    pass


async def submit(
    *, org_id: int, user_email: str, category: FeedbackCategory, message: str,
    call_ids: list[str], endpoint_id: str | None,
) -> int:
    async with session_maker() as db:
        await ratestore.sweep(db, RATE_NAMESPACE)
        if not await ratestore.rate_check(
            db, RATE_NAMESPACE, [(str(org_id), RATE_MAX)], RATE_WINDOW_S,
        ):
            await db.commit()
            raise FeedbackRateLimited
        # Only look inside this team. Missing audit rows and delayed reports remain reportable;
        # unknown references never grant access or count as verified attribution.
        verified: set[str] = set()
        if call_ids:
            verified.update((await db.execute(select(CallRecord.call_ref).where(
                CallRecord.org_id == org_id, CallRecord.call_ref.in_(call_ids),
            ))).scalars())
            verified.update((await db.execute(select(LedgerEntry.call_id).where(
                LedgerEntry.org_id == org_id, LedgerEntry.call_id.in_(call_ids),
            ))).scalars())
        row = feedback.add(
            db, org_id=org_id, user_email=user_email, category=category, message=message,
            call_ids=call_ids, verified_call_ids=[ref for ref in call_ids if ref in verified],
            endpoint_id=endpoint_id,
        )
        await db.flush()
        feedback_id = row.id
        await db.commit()
        return feedback_id
