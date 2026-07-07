"""Control-plane API.

Two audiences:
  1. The agent (media plane) — /agent/* endpoints backing each LLM tool.
     These must be FAST (<100ms): the caller is waiting on the line.
  2. The owner — /owner/* endpoints (KB edits, digest trigger, call log).

Run:  uvicorn api.main:app --reload --port 8080
"""
import os
from contextlib import asynccontextmanager
from datetime import date as date_t, datetime, time as time_t

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, create_engine, select

from . import availability, notify
from .models import (
    Business, CallOutcome, CallRecord, KBEntry, Message, Reservation, ServicePeriod, Urgency,
)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./receptionist.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    yield


app = FastAPI(title="Receptionist Control Plane", lifespan=lifespan)


def db():
    with Session(engine) as session:
        yield session


def get_business(session: Session, slug: str) -> Business:
    biz = session.exec(select(Business).where(Business.slug == slug)).first()
    if not biz:
        raise HTTPException(404, f"unknown business '{slug}'")
    return biz


# ======================= agent-facing (tool backends) =======================

@app.get("/agent/{slug}/context")
def agent_context(slug: str, session: Session = Depends(db)):
    """Everything the agent needs at call start, in ONE round-trip:
    business profile + full KB + today's hours. Injected into the system
    prompt so most FAQ turns need zero tool calls (fastest possible answer)."""
    biz = get_business(session, slug)
    kb = session.exec(select(KBEntry).where(KBEntry.business_id == biz.id)).all()
    periods = session.exec(
        select(ServicePeriod).where(ServicePeriod.business_id == biz.id)
    ).all()
    return {
        "business": biz.model_dump(exclude={"id"}),
        "kb": [{"topic": k.topic, "answer": k.answer} for k in kb],
        "hours": [
            {"day": p.day_of_week, "name": p.name,
             "opens": p.opens.isoformat(), "last_seating": p.last_seating.isoformat(),
             "closes": p.closes.isoformat()}
            for p in periods
        ],
    }


class AvailabilityQuery(BaseModel):
    date: date_t
    time: time_t
    party_size: int


@app.post("/agent/{slug}/availability")
def check_availability(slug: str, q: AvailabilityQuery, session: Session = Depends(db)):
    biz = get_business(session, slug)
    res = availability.check_availability(session, biz, q.date, q.time, q.party_size)
    return {"available": res.available, "reason": res.reason, "alternatives": res.alternatives or []}


class ReservationCreate(BaseModel):
    date: date_t
    time: time_t
    party_size: int
    guest_name: str
    guest_phone: str
    notes: str = ""
    call_id: str | None = None


@app.post("/agent/{slug}/reservations")
def create_reservation(slug: str, r: ReservationCreate, session: Session = Depends(db)):
    biz = get_business(session, slug)
    # Re-check capacity at write time (two callers can race on Friday 7pm).
    res = availability.check_availability(session, biz, r.date, r.time, r.party_size)
    if not res.available:
        return {"created": False, "reason": res.reason, "alternatives": res.alternatives or []}
    row = Reservation(
        business_id=biz.id, on_date=r.date, at_time=r.time, party_size=r.party_size,
        guest_name=r.guest_name, guest_phone=r.guest_phone, notes=r.notes, call_id=r.call_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    notify.notify_reservation(biz, row)
    return {"created": True, "reservation_id": row.id}


class MessageCreate(BaseModel):
    caller_name: str
    caller_phone: str
    reason: str
    urgency: Urgency = Urgency.normal
    call_id: str | None = None


@app.post("/agent/{slug}/messages")
def take_message(slug: str, m: MessageCreate, session: Session = Depends(db)):
    biz = get_business(session, slug)
    row = Message(business_id=biz.id, **m.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    notify.notify_message(biz, row)
    return {"created": True, "message_id": row.id}


class CallReport(BaseModel):
    call_id: str
    caller_phone: str = ""
    outcome: CallOutcome | None = None
    summary: str = ""
    transcript: str = ""


@app.post("/agent/{slug}/calls")
def report_call(slug: str, c: CallReport, session: Session = Depends(db)):
    """Called by the agent at call end (fire-and-forget; never blocks audio)."""
    biz = get_business(session, slug)
    row = CallRecord(
        business_id=biz.id, call_id=c.call_id, caller_phone=c.caller_phone,
        outcome=c.outcome, summary=c.summary, transcript=c.transcript,
        ended_at=datetime.utcnow(),
    )
    session.add(row)
    session.commit()
    return {"ok": True}


# =========================== owner-facing ===========================

class KBUpsert(BaseModel):
    topic: str
    answer: str


@app.post("/owner/{slug}/kb")
def upsert_kb(slug: str, entry: KBUpsert, session: Session = Depends(db)):
    biz = get_business(session, slug)
    row = session.exec(
        select(KBEntry).where(KBEntry.business_id == biz.id, KBEntry.topic == entry.topic)
    ).first()
    if row:
        row.answer, row.updated_at = entry.answer, datetime.utcnow()
    else:
        row = KBEntry(business_id=biz.id, **entry.model_dump())
    session.add(row)
    session.commit()
    return {"ok": True}


@app.post("/owner/{slug}/digest")
def trigger_digest(slug: str, session: Session = Depends(db)):
    biz = get_business(session, slug)
    return {"digest": notify.send_digest(session, biz)}


@app.get("/owner/{slug}/calls")
def list_calls(slug: str, limit: int = 50, session: Session = Depends(db)):
    biz = get_business(session, slug)
    rows = session.exec(
        select(CallRecord).where(CallRecord.business_id == biz.id)
        .order_by(CallRecord.started_at.desc()).limit(limit)
    ).all()
    return rows


@app.get("/owner/{slug}/reservations")
def list_reservations(slug: str, on: date_t | None = None, session: Session = Depends(db)):
    biz = get_business(session, slug)
    stmt = select(Reservation).where(Reservation.business_id == biz.id)
    if on:
        stmt = stmt.where(Reservation.on_date == on)
    return session.exec(stmt.order_by(Reservation.on_date, Reservation.at_time)).all()
