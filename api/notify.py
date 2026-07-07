"""Notifications: instant SMS alerts + the daily digest (the signature artifact).

Dev mode (no Twilio creds): prints to console so the whole system runs free
on a laptop. Prod: Twilio SMS. Swap provider behind send_sms() only.
"""
import os
from datetime import datetime, timedelta

from sqlmodel import Session, select

from .models import Business, CallOutcome, CallRecord, Message, Reservation, ReservationStatus

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "")


def send_sms(to: str, body: str) -> None:
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and to):
        print(f"\n[SMS -> {to or 'unset'}]\n{body}\n")  # dev fallback
        return
    from twilio.rest import Client  # lazy import; optional dep in dev

    Client(TWILIO_SID, TWILIO_TOKEN).messages.create(to=to, from_=TWILIO_FROM, body=body)


# ---------- instant alerts ----------

def notify_reservation(biz: Business, r: Reservation) -> None:
    send_sms(
        biz.owner_mobile,
        f"📅 New booking @ {biz.name}: {r.guest_name}, party of {r.party_size}, "
        f"{r.on_date.strftime('%a %d %b')} {r.at_time.strftime('%H:%M')}. "
        f"Ph {r.guest_phone}." + (f" Note: {r.notes}" if r.notes else ""),
    )
    if r.guest_phone:
        send_sms(
            r.guest_phone,
            f"{biz.name}: your table for {r.party_size} is confirmed for "
            f"{r.on_date.strftime('%A %d %B')} at {r.at_time.strftime('%H:%M')}. "
            f"Reply or call to change.",
        )


def notify_message(biz: Business, m: Message) -> None:
    prefix = "⚠️ URGENT — " if m.urgency == "urgent" else "✉️ "
    send_sms(
        biz.owner_mobile,
        f"{prefix}Message @ {biz.name}: {m.caller_name} ({m.caller_phone}) — {m.reason}",
    )


# ---------- daily digest ----------

def compose_digest(session: Session, biz: Business, day: datetime | None = None) -> str:
    day = day or datetime.utcnow()
    start, end = day - timedelta(hours=24), day

    calls = session.exec(
        select(CallRecord).where(
            CallRecord.business_id == biz.id,
            CallRecord.started_at >= start,
            CallRecord.started_at < end,
        )
    ).all()
    junk = [c for c in calls if c.outcome == CallOutcome.junk]

    msg_rows = session.exec(
        select(Message).where(Message.business_id == biz.id, Message.created_at >= start)
    ).all()

    res_rows = session.exec(
        select(Reservation).where(
            Reservation.business_id == biz.id,
            Reservation.created_at >= start,
            Reservation.status == ReservationStatus.confirmed,
        )
    ).all()
    covers = sum(r.party_size for r in res_rows)

    urgent = session.exec(
        select(Message).where(
            Message.business_id == biz.id,
            Message.created_at >= start,
            Message.urgency == "urgent",
        )
    ).all()

    lines = [
        f"📞 Today @ {biz.name}:",
        f"• {len(calls)} calls answered (0 missed)",
        f"• {len(res_rows)} bookings ({covers} covers)",
        f"• {len(msg_rows)} messages" + (f" ({len(urgent)} urgent ⚠️)" if urgent else ""),
    ]
    if junk:
        lines.append(f"• {len(junk)} spam calls screened")
    return "\n".join(lines)


def send_digest(session: Session, biz: Business) -> str:
    body = compose_digest(session, biz)
    send_sms(biz.owner_mobile, body)
    return body
