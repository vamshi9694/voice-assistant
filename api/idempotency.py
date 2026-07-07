"""Server-side idempotency for mutating agent tools.

Safety rule: all mutating tool calls carry an idempotency_key. A replayed key
(LLM retry, duplicated tool call, network retry) returns the FIRST response
instead of creating a second reservation/order/message.

Usage in an endpoint:
    cached = idem_get(session, biz.id, key, "reservations")
    if cached is not None:
        return cached
    ... do the work, build `resp` dict ...
    idem_put(session, biz.id, key, "reservations", resp)   # before commit is fine
"""
import json
from typing import Optional

from sqlmodel import Session, select

from .models import IdempotencyRecord


def idem_get(session: Session, business_id: int, key: Optional[str], endpoint: str) -> Optional[dict]:
    if not key:
        return None
    row = session.exec(
        select(IdempotencyRecord).where(
            IdempotencyRecord.business_id == business_id,
            IdempotencyRecord.key == key,
            IdempotencyRecord.endpoint == endpoint,
        )
    ).first()
    if row:
        try:
            resp = json.loads(row.response_json)
        except Exception:
            return None
        resp["idempotent_replay"] = True
        return resp
    return None


def idem_put(session: Session, business_id: int, key: Optional[str], endpoint: str, response: dict) -> None:
    if not key:
        return
    session.add(IdempotencyRecord(
        business_id=business_id, key=key, endpoint=endpoint,
        response_json=json.dumps(response, default=str),
    ))
