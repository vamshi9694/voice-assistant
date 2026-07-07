"""Tenant routing + tenant/number management.

- /agent/resolve            number -> tenant (the media plane calls this at call start)
- /admin/*                  platform-owner operations (tenants, numbers)
- PATCH /owner/{slug}/business   client-editable business config

Auth arrives in the dashboard phase; these routes are loopback-only in prod
(control plane binds 127.0.0.1) and are additionally gated there.
"""
import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .models import Business, PhoneNumber, TenantStatus


def _norm_e164(raw: str) -> str:
    """Normalize a dialed number to +digits. Twilio sends E.164 already;
    tolerate spaces/dashes/parens from manual entry."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits and not digits.startswith("+"):
        digits = "+" + digits
    return digits


def wire(app, db):
    """Attach routes with the app's db dependency (avoids circular import).

    # ------------------------- agent-facing -------------------------
    """

    @app.get("/agent/resolve")
    def resolve_number(to: str, session: Session = Depends(db)):
        """Called-number -> tenant. The ONLY tenant selector for phone calls."""
        e164 = _norm_e164(to)
        num = session.exec(
            select(PhoneNumber).where(PhoneNumber.e164 == e164, PhoneNumber.active == True)  # noqa: E712
        ).first()
        if not num:
            raise HTTPException(404, f"no tenant for number '{e164}'")
        biz = session.get(Business, num.business_id)
        if not biz or biz.status != TenantStatus.active:
            raise HTTPException(423, f"tenant for '{e164}' is not active")
        return {"slug": biz.slug, "business_id": biz.id, "name": biz.name}

    # ------------------------- admin (platform owner) -------------------------

    class TenantCreate(BaseModel):
        slug: str
        name: str
        timezone: str = "Australia/Melbourne"
        industry: str = "restaurant"

    @app.post("/admin/tenants")
    def create_tenant(t: TenantCreate, session: Session = Depends(db)):
        if session.exec(select(Business).where(Business.slug == t.slug)).first():
            raise HTTPException(409, f"slug '{t.slug}' exists")
        biz = Business(**t.model_dump(), status=TenantStatus.onboarding)
        session.add(biz)
        session.commit()
        session.refresh(biz)
        return {"ok": True, "business_id": biz.id, "slug": biz.slug}

    @app.get("/admin/tenants")
    def list_tenants(session: Session = Depends(db)):
        rows = session.exec(select(Business).order_by(Business.name)).all()
        numbers = session.exec(select(PhoneNumber)).all()
        by_biz: dict[int, list] = {}
        for n in numbers:
            by_biz.setdefault(n.business_id, []).append(n.e164)
        return [
            {"slug": b.slug, "name": b.name, "status": b.status,
             "industry": b.industry, "numbers": by_biz.get(b.id, [])}
            for b in rows
        ]

    class TenantStatusUpdate(BaseModel):
        status: TenantStatus

    @app.post("/admin/tenants/{slug}/status")
    def set_tenant_status(slug: str, u: TenantStatusUpdate, session: Session = Depends(db)):
        biz = session.exec(select(Business).where(Business.slug == slug)).first()
        if not biz:
            raise HTTPException(404, "unknown tenant")
        biz.status = u.status
        session.add(biz)
        session.commit()
        return {"ok": True, "status": biz.status}

    class NumberAssign(BaseModel):
        e164: str
        slug: str
        label: str = ""

    @app.post("/admin/numbers")
    def assign_number(n: NumberAssign, session: Session = Depends(db)):
        biz = session.exec(select(Business).where(Business.slug == n.slug)).first()
        if not biz:
            raise HTTPException(404, f"unknown tenant '{n.slug}'")
        e164 = _norm_e164(n.e164)
        existing = session.exec(select(PhoneNumber).where(PhoneNumber.e164 == e164)).first()
        if existing:
            existing.business_id, existing.label, existing.active = biz.id, n.label, True
            session.add(existing)
        else:
            session.add(PhoneNumber(e164=e164, business_id=biz.id, label=n.label))
        session.commit()
        return {"ok": True, "e164": e164, "slug": n.slug}

    @app.get("/admin/numbers")
    def list_numbers(session: Session = Depends(db)):
        rows = session.exec(select(PhoneNumber)).all()
        biz_by_id = {b.id: b for b in session.exec(select(Business)).all()}
        return [
            {"e164": r.e164, "slug": getattr(biz_by_id.get(r.business_id), "slug", "?"),
             "label": r.label, "active": r.active}
            for r in rows
        ]

    @app.post("/admin/numbers/{e164}/release")
    def release_number(e164: str, session: Session = Depends(db)):
        row = session.exec(select(PhoneNumber).where(PhoneNumber.e164 == _norm_e164(e164))).first()
        if not row:
            raise HTTPException(404, "unknown number")
        row.active = False
        session.add(row)
        session.commit()
        return {"ok": True}

    # ------------------------- client-editable config -------------------------

    # Fields a restaurant may edit from its own dashboard. Everything else
    # (slug, status, industry) is admin-only.
    CLIENT_EDITABLE = {
        "name", "timezone", "address", "website", "phone_forward_to",
        "owner_mobile", "manager_phone", "manager_email", "greeting",
        "covers_per_slot", "max_party_size", "slot_minutes", "reservation_notes",
        "orders_enabled", "order_pickup_minutes", "order_policy_notes",
        "default_language", "enabled_languages", "auto_detect_language",
        "voice_map", "language_fallback", "persona_notes", "escalation_rules",
    }

    @app.patch("/owner/{slug}/business")
    def patch_business(slug: str, patch: dict, session: Session = Depends(db)):
        biz = session.exec(select(Business).where(Business.slug == slug)).first()
        if not biz:
            raise HTTPException(404, "unknown tenant")
        rejected = [k for k in patch if k not in CLIENT_EDITABLE]
        if rejected:
            raise HTTPException(422, f"fields not editable: {rejected}")
        for k, v in patch.items():
            setattr(biz, k, v)
        session.add(biz)
        session.commit()
        return {"ok": True, "updated": list(patch.keys())}

    @app.get("/owner/{slug}/business")
    def get_business_config(slug: str, session: Session = Depends(db)):
        biz = session.exec(select(Business).where(Business.slug == slug)).first()
        if not biz:
            raise HTTPException(404, "unknown tenant")
        return biz.model_dump(exclude={"id"})
