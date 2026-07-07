"""Control-plane API.

Two audiences:
  1. The agent (media plane) — /agent/* endpoints backing each LLM tool.
     These must be FAST (<100ms): the caller is waiting on the line.
  2. The owner — /owner/* endpoints (KB edits, digest trigger, call log).

Run:  uvicorn api.main:app --reload --port 8080
"""
import os
from contextlib import asynccontextmanager
from datetime import date as date_t, datetime, time as time_t, timedelta

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, create_engine, select

from . import availability, notify
from .idempotency import idem_get, idem_put
from .models import (
    Business, CallOutcome, CallRecord, KBEntry, Message, Reservation, ReservationStatus,
    ServicePeriod, Urgency,
)

PHONE_DIGITS_RE = __import__("re").compile(r"\d")


def require_callback_phone(raw: str) -> str:
    """Safety rule: a usable callback number is required for reservations,
    orders, and messages. Accepts E.164 or national format; requires >= 10
    digits (or 9 starting with 0 dropped after a country code, e.g. AU mobiles
    +61 4xx xxx xxx). Returns normalized digits, raises 422 otherwise."""
    digits = "".join(PHONE_DIGITS_RE.findall(raw or ""))
    if raw and raw.strip().startswith("+") and len(digits) >= 10:
        return "+" + digits
    if len(digits) == 10 or (len(digits) == 9 and not digits.startswith("0")):
        return digits
    if len(digits) == 11 and digits.startswith(("0", "1")):
        return digits
    raise HTTPException(422, "callback phone must be a valid 10-digit number")

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


# Tenant routing + admin + business-config routes (api/tenants.py)
from . import tenants as _tenants  # noqa: E402
_tenants.wire(app, db)


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
        "languages": {
            "default": biz.default_language,
            "enabled": biz.enabled_langs(),
            "auto_detect": biz.auto_detect_language,
            "voices": biz.voices(),
            "fallback": biz.language_fallback,
        },
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
    idempotency_key: str | None = None


@app.post("/agent/{slug}/reservations")
def create_reservation(slug: str, r: ReservationCreate, session: Session = Depends(db)):
    biz = get_business(session, slug)
    cached = idem_get(session, biz.id, r.idempotency_key, "reservations")
    if cached is not None:
        return cached
    r.guest_phone = require_callback_phone(r.guest_phone)
    # Large-party threshold: never auto-book above it — the agent should take
    # a message / escalate instead. Enforced server-side, not just in prompt.
    if r.party_size > biz.max_party_size:
        return {
            "created": False,
            "reason": f"party of {r.party_size} exceeds the {biz.max_party_size}-person limit; "
                      "take a message for the manager instead",
        }
    # Re-check capacity at write time (two callers can race on Friday 7pm).
    res = availability.check_availability(session, biz, r.date, r.time, r.party_size)
    if not res.available:
        resp = {"created": False, "reason": res.reason, "alternatives": res.alternatives or []}
        idem_put(session, biz.id, r.idempotency_key, "reservations", resp)
        session.commit()
        return resp
    row = Reservation(
        business_id=biz.id, on_date=r.date, at_time=r.time, party_size=r.party_size,
        guest_name=r.guest_name, guest_phone=r.guest_phone, notes=r.notes, call_id=r.call_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    resp = {"created": True, "reservation_id": row.id}
    idem_put(session, biz.id, r.idempotency_key, "reservations", resp)
    session.commit()
    notify.notify_reservation(biz, row)
    return resp


class MessageCreate(BaseModel):
    caller_name: str
    caller_phone: str
    reason: str
    urgency: Urgency = Urgency.normal
    call_id: str | None = None
    idempotency_key: str | None = None


@app.post("/agent/{slug}/messages")
def take_message(slug: str, m: MessageCreate, session: Session = Depends(db)):
    biz = get_business(session, slug)
    cached = idem_get(session, biz.id, m.idempotency_key, "messages")
    if cached is not None:
        return cached
    m.caller_phone = require_callback_phone(m.caller_phone)
    row = Message(business_id=biz.id, **m.model_dump(exclude={"idempotency_key"}))
    session.add(row)
    session.commit()
    session.refresh(row)
    resp = {"created": True, "message_id": row.id}
    idem_put(session, biz.id, m.idempotency_key, "messages", resp)
    session.commit()
    notify.notify_message(biz, row)
    return resp


class CallReport(BaseModel):
    call_id: str
    caller_phone: str = ""
    called_number: str = ""
    language: str = ""
    outcome: CallOutcome | None = None
    summary: str = ""
    transcript: str = ""


@app.post("/agent/{slug}/calls")
def report_call(slug: str, c: CallReport, session: Session = Depends(db)):
    """Called by the agent at call end (fire-and-forget; never blocks audio)."""
    biz = get_business(session, slug)
    row = CallRecord(
        business_id=biz.id, call_id=c.call_id, caller_phone=c.caller_phone,
        called_number=c.called_number, language=c.language,
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


# ======================= dashboard-facing (read + manage) =======================

@app.get("/owner/businesses")
def list_businesses(session: Session = Depends(db)):
    """All tenants — powers the dashboard's business switcher."""
    rows = session.exec(select(Business).order_by(Business.name)).all()
    return [{"slug": b.slug, "name": b.name} for b in rows]


@app.get("/owner/{slug}/messages")
def list_messages(slug: str, limit: int = 100, session: Session = Depends(db)):
    biz = get_business(session, slug)
    return session.exec(
        select(Message).where(Message.business_id == biz.id)
        .order_by(Message.created_at.desc()).limit(limit)
    ).all()


@app.get("/owner/{slug}/kb")
def list_kb(slug: str, session: Session = Depends(db)):
    biz = get_business(session, slug)
    return session.exec(
        select(KBEntry).where(KBEntry.business_id == biz.id).order_by(KBEntry.topic)
    ).all()


@app.post("/owner/{slug}/kb/delete")
def delete_kb(slug: str, entry: dict, session: Session = Depends(db)):
    biz = get_business(session, slug)
    row = session.exec(
        select(KBEntry).where(KBEntry.business_id == biz.id, KBEntry.topic == entry.get("topic"))
    ).first()
    if row:
        session.delete(row)
        session.commit()
    return {"ok": True}


@app.get("/owner/{slug}/hours")
def list_hours(slug: str, session: Session = Depends(db)):
    biz = get_business(session, slug)
    rows = session.exec(
        select(ServicePeriod).where(ServicePeriod.business_id == biz.id)
    ).all()
    return [
        {"id": p.id, "day_of_week": p.day_of_week, "name": p.name,
         "opens": p.opens.isoformat(), "last_seating": p.last_seating.isoformat(),
         "closes": p.closes.isoformat()}
        for p in sorted(rows, key=lambda x: (x.day_of_week, x.opens))
    ]


@app.post("/owner/{slug}/reservations/{res_id}/cancel")
def cancel_reservation(slug: str, res_id: int, session: Session = Depends(db)):
    biz = get_business(session, slug)
    row = session.exec(
        select(Reservation).where(
            Reservation.id == res_id, Reservation.business_id == biz.id
        )
    ).first()
    if not row:
        raise HTTPException(404, "reservation not found")
    row.status = ReservationStatus.cancelled
    session.add(row)
    session.commit()
    return {"ok": True}


@app.get("/owner/{slug}/overview")
def owner_overview(slug: str, session: Session = Depends(db)):
    """Headline counts for the dashboard overview cards."""
    biz = get_business(session, slug)
    today = date_t.today()
    day_ago = datetime.utcnow() - timedelta(hours=24)

    bookings_today = len(session.exec(
        select(Reservation).where(
            Reservation.business_id == biz.id, Reservation.on_date == today,
            Reservation.status == ReservationStatus.confirmed,
        )
    ).all())
    upcoming = len(session.exec(
        select(Reservation).where(
            Reservation.business_id == biz.id, Reservation.on_date >= today,
            Reservation.status == ReservationStatus.confirmed,
        )
    ).all())
    messages = session.exec(select(Message).where(Message.business_id == biz.id)).all()
    urgent = len([m for m in messages if m.urgency == Urgency.urgent])
    calls_24h = len(session.exec(
        select(CallRecord).where(
            CallRecord.business_id == biz.id, CallRecord.started_at >= day_ago
        )
    ).all())
    return {
        "business": biz.name,
        "bookings_today": bookings_today,
        "upcoming_bookings": upcoming,
        "messages_total": len(messages),
        "messages_urgent": urgent,
        "calls_24h": calls_24h,
    }
