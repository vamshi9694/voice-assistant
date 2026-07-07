"""Control-plane data models (SQLModel = Pydantic + SQLAlchemy).

One SQLite file in dev; swap DATABASE_URL to Postgres in prod. Multi-tenant
from day one via business_id on every table.
"""
from datetime import datetime, date, time
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class Business(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)          # "luigis-carlton"
    name: str                                            # "Luigi's Trattoria"
    timezone: str = "Australia/Melbourne"
    address: str = ""
    phone_forward_to: str = ""                           # staff line for transfers
    owner_mobile: str = ""                               # digest + urgent SMS target
    greeting: str = ""                                   # opening line override
    # Reservation capacity rules (simple v1 model)
    covers_per_slot: int = 12                            # max new covers per 15-min slot
    max_party_size: int = 8                              # larger -> take message / transfer
    slot_minutes: int = 15
    persona_notes: str = ""                              # tone/style hints for the agent


class ServicePeriod(SQLModel, table=True):
    """Opening hours as service periods, not open/close pairs (handles split
    lunch/dinner service and kitchen-closes-early)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    day_of_week: int                                     # 0=Mon .. 6=Sun
    name: str = "dinner"                                 # lunch / dinner / all-day
    opens: time
    last_seating: time                                   # last reservation time
    closes: time


class HolidayOverride(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    on_date: date
    closed: bool = True
    note: str = ""                                       # "Closed Anzac Day"


class KBEntry(SQLModel, table=True):
    """Structured FAQ knowledge. Small enough per-tenant to inject wholesale
    into the system prompt in v1 (typical restaurant KB < 2k tokens)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    topic: str                                           # "parking", "byo", "gluten free"
    answer: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ReservationStatus(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"


class Reservation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    on_date: date = Field(index=True)
    at_time: time
    party_size: int
    guest_name: str
    guest_phone: str
    notes: str = ""                                      # "window seat", "anniversary"
    status: ReservationStatus = ReservationStatus.confirmed
    created_at: datetime = Field(default_factory=datetime.utcnow)
    call_id: Optional[str] = None                        # provenance


class Urgency(str, Enum):
    normal = "normal"
    urgent = "urgent"


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    caller_name: str
    caller_phone: str
    reason: str
    urgency: Urgency = Urgency.normal
    created_at: datetime = Field(default_factory=datetime.utcnow)
    call_id: Optional[str] = None


class CallOutcome(str, Enum):
    faq = "faq"
    reservation = "reservation"
    message = "message"
    transfer = "transfer"
    junk = "junk"
    abandoned = "abandoned"


class CallRecord(SQLModel, table=True):
    """One row per call; the digest is an aggregation over these."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    call_id: str = Field(index=True)                     # transport call sid / uuid
    caller_phone: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    outcome: Optional[CallOutcome] = None
    summary: str = ""                                    # one-line LLM summary
    transcript: str = ""                                 # appended turn by turn
