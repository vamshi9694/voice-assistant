"""Availability engine (v1 internal book).

Deliberately simple: capacity = covers_per_slot per slot_minutes window within
a service period. Good enough for independents; swap this module for an
OpenTable/SevenRooms adapter in v2 without touching the agent.

Key product behavior: when the requested time is full, ALWAYS compute nearest
alternatives (this is the 15-30% booking-conversion save).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from .models import Business, HolidayOverride, Reservation, ReservationStatus, ServicePeriod


@dataclass
class AvailabilityResult:
    available: bool
    reason: str = ""                       # "closed", "full", "party_too_large", "outside_hours"
    alternatives: list[str] | None = None  # ["18:45", "20:15"]


def _slots_for_day(session: Session, biz: Business, on_date: date) -> list[time]:
    """All bookable slot start times for a date, honoring holiday overrides."""
    holiday = session.exec(
        select(HolidayOverride).where(
            HolidayOverride.business_id == biz.id, HolidayOverride.on_date == on_date
        )
    ).first()
    if holiday and holiday.closed:
        return []

    periods = session.exec(
        select(ServicePeriod).where(
            ServicePeriod.business_id == biz.id,
            ServicePeriod.day_of_week == on_date.weekday(),
        )
    ).all()

    slots: list[time] = []
    step = timedelta(minutes=biz.slot_minutes)
    for p in periods:
        t = datetime.combine(on_date, p.opens)
        last = datetime.combine(on_date, p.last_seating)
        while t <= last:
            slots.append(t.time())
            t += step
    return sorted(slots)


def _covers_booked(session: Session, biz: Business, on_date: date, at: time) -> int:
    rows = session.exec(
        select(Reservation).where(
            Reservation.business_id == biz.id,
            Reservation.on_date == on_date,
            Reservation.at_time == at,
            Reservation.status == ReservationStatus.confirmed,
        )
    ).all()
    return sum(r.party_size for r in rows)


def check_availability(
    session: Session, biz: Business, on_date: date, at: time, party_size: int
) -> AvailabilityResult:
    if party_size > biz.max_party_size:
        return AvailabilityResult(False, reason="party_too_large")

    slots = _slots_for_day(session, biz, on_date)
    if not slots:
        return AvailabilityResult(False, reason="closed")
    if at not in slots:
        # Requested time isn't a valid slot (outside hours / off-grid time):
        # snap to nearest and treat as an alternatives problem.
        return AvailabilityResult(
            False, reason="outside_hours", alternatives=_nearest_open(session, biz, on_date, at, party_size, slots)
        )

    if _covers_booked(session, biz, on_date, at) + party_size <= biz.covers_per_slot:
        return AvailabilityResult(True)

    return AvailabilityResult(
        False, reason="full", alternatives=_nearest_open(session, biz, on_date, at, party_size, slots)
    )


def _nearest_open(
    session: Session, biz: Business, on_date: date, want: time,
    party_size: int, slots: list[time], limit: int = 2,
) -> list[str]:
    """Nearest slots (by absolute distance from requested time) with capacity."""
    want_dt = datetime.combine(on_date, want)

    def dist(s: time) -> float:
        return abs((datetime.combine(on_date, s) - want_dt).total_seconds())

    out = []
    for s in sorted(slots, key=dist):
        if s == want:
            continue
        if _covers_booked(session, biz, on_date, s) + party_size <= biz.covers_per_slot:
            out.append(s.strftime("%H:%M"))
        if len(out) >= limit:
            break
    return out
