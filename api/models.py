"""Control-plane data models (SQLModel = Pydantic + SQLAlchemy).

One SQLite file in dev; swap DATABASE_URL to Postgres in prod. Multi-tenant
from day one via business_id on every table. JSON-ish config fields are stored
as JSON strings (portable across SQLite/Postgres without dialect types).
"""
import json
from datetime import datetime, date, time
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class TenantStatus(str, Enum):
    active = "active"
    paused = "paused"          # number answers with a static "call back later"
    onboarding = "onboarding"  # config editable, calls not yet routed


class Business(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)          # "luigis-carlton"
    name: str                                            # "Luigi's Trattoria"
    status: TenantStatus = TenantStatus.active
    industry: str = "restaurant"                         # expandable later
    timezone: str = "Australia/Melbourne"
    address: str = ""
    website: str = ""                                    # also the approved crawl domain
    phone_forward_to: str = ""                           # staff line for transfers
    owner_mobile: str = ""                               # digest + urgent SMS target
    manager_phone: str = ""                              # escalation target (falls back to owner_mobile)
    manager_email: str = ""                              # escalation email
    greeting: str = ""                                   # opening line override
    # Reservation capacity rules (simple v1 model)
    covers_per_slot: int = 12                            # max new covers per 15-min slot
    max_party_size: int = 8                              # large-party threshold: larger -> message/escalate
    slot_minutes: int = 15
    reservation_notes: str = ""                          # free-form rules spoken policy ("no bookings Fri after 8")
    # Phone orders
    orders_enabled: bool = False                         # takeout/phone-order policy switch
    order_pickup_minutes: int = 20                       # quoted pickup lead time
    order_policy_notes: str = ""                         # "pickup only, pay in store", etc.
    # Languages (per-tenant; overrides the old LANGUAGE env var)
    default_language: str = "en"                         # BCP-47-ish short code
    enabled_languages: str = '["en"]'                    # JSON list, e.g. '["en","es"]'
    auto_detect_language: bool = False                   # detect caller language per utterance
    voice_map: str = "{}"                                # JSON {lang: tts_voice_id}
    language_fallback: str = (
        "I'm sorry, I can only help in English right now — I can take a message "
        "for the manager to call you back."
    )
    # Persona + escalation
    persona_notes: str = ""                              # tone/style hints for the agent
    escalation_rules: str = ""                           # free-form: when to escalate to manager

    # -- helpers (not columns) --
    def enabled_langs(self) -> list[str]:
        try:
            v = json.loads(self.enabled_languages or "[]")
            return v if isinstance(v, list) and v else [self.default_language]
        except Exception:
            return [self.default_language]

    def voices(self) -> dict:
        try:
            v = json.loads(self.voice_map or "{}")
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}


class PhoneNumber(SQLModel, table=True):
    """Inbound number -> tenant routing. One tenant may own several numbers
    (locations, marketing lines); a number maps to exactly one tenant."""
    id: Optional[int] = Field(default=None, primary_key=True)
    e164: str = Field(index=True, unique=True)           # "+61370000000"
    business_id: int = Field(index=True, foreign_key="business.id")
    label: str = ""                                      # "main line", "catering"
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IdempotencyRecord(SQLModel, table=True):
    """Server-side dedupe for mutating agent tools. (business_id, key) is
    unique; a replayed tool call returns the stored response instead of
    double-booking / double-ordering / double-messaging."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True)
    key: str = Field(index=True)                         # idempotency_key from the tool call
    endpoint: str = ""                                   # "reservations" | "orders" | "messages"
    response_json: str = "{}"                            # stored first response
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
    source_url: str = ""                                 # provenance when crawled
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MenuItem(SQLModel, table=True):
    """The tenant's live menu — the ONLY source of truth for what exists.
    The agent must never invent items; orders are validated against this
    table server-side."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    section: str = "Mains"                               # "Starters", "Pasta", "Drinks"
    name: str
    description: str = ""
    price: float = 0.0
    dietary: str = ""                                    # "GF, V", free-form tags
    available: bool = True                               # 86'd items stay but don't sell
    source: str = "manual"                               # manual | csv | pdf | image | website
    source_url: str = ""                                 # provenance when ingested
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OrderStatus(str, Enum):
    received = "received"
    ready = "ready"
    picked_up = "picked_up"
    cancelled = "cancelled"


class Order(SQLModel, table=True):
    """Phone pickup order. items_json: [{"name","qty","price","notes"}] —
    validated against MenuItem at create time (no invented items, current
    prices)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    business_id: int = Field(index=True, foreign_key="business.id")
    guest_name: str
    guest_phone: str
    items_json: str = "[]"
    total: float = 0.0
    pickup_minutes: int = 20
    notes: str = ""
    status: OrderStatus = OrderStatus.received
    created_at: datetime = Field(default_factory=datetime.utcnow)
    call_id: Optional[str] = None


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
    order = "order"
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
    called_number: str = ""                              # which tenant number was dialed
    language: str = ""                                   # language the call settled into
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    outcome: Optional[CallOutcome] = None
    summary: str = ""                                    # one-line LLM summary
    transcript: str = ""                                 # appended turn by turn
