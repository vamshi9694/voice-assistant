"""Call QA metrics: ingest (from the media plane) + summaries (dashboards).

The media plane batches CallEvent rows per call (agent/qa.py); the control
plane adds `duplicate` events itself when idempotency catches a replay.
"""
from datetime import datetime, timedelta
from statistics import mean

from fastapi import Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from .models import Business, CallEvent, CallEventKind, CallRecord

LATENCY_KINDS = {
    CallEventKind.stt_ttfb: "avg_stt_ms",
    CallEventKind.llm_ttfb: "avg_llm_ms",
    CallEventKind.tts_ttfb: "avg_tts_ms",
    CallEventKind.tool_latency: "avg_tool_ms",
    CallEventKind.turn_e2e: "avg_turn_ms",
}
COUNT_KINDS = {
    CallEventKind.dead_air: "dead_air",
    CallEventKind.hello_retry: "hello_retries",
    CallEventKind.tool_failure: "tool_failures",
    CallEventKind.duplicate: "duplicates",
    CallEventKind.low_confidence: "low_confidence",
}


def _tenant_summary(session: Session, biz: Business, since: datetime) -> dict:
    events = session.exec(select(CallEvent).where(
        CallEvent.business_id == biz.id, CallEvent.created_at >= since)).all()
    calls = session.exec(select(CallRecord).where(
        CallRecord.business_id == biz.id, CallRecord.started_at >= since)).all()

    out = {"slug": biz.slug, "name": biz.name, "calls": len(calls)}
    for kind, key in LATENCY_KINDS.items():
        vals = [e.value_ms for e in events if e.kind == kind and e.value_ms is not None]
        out[key] = round(mean(vals), 1) if vals else None
    for kind, key in COUNT_KINDS.items():
        out[key] = sum(1 for e in events if e.kind == kind)
    return out


def wire(app, db, get_business):

    class EventIn(BaseModel):
        call_id: str = ""
        kind: CallEventKind
        value_ms: float | None = None
        detail: str = ""

    @app.post("/agent/{slug}/metrics")
    def ingest_metrics(slug: str, events: list[EventIn], session: Session = Depends(db)):
        biz = get_business(session, slug)
        for e in events[:200]:
            session.add(CallEvent(business_id=biz.id, **e.model_dump()))
        session.commit()
        return {"ok": True, "ingested": min(len(events), 200)}

    @app.get("/owner/{slug}/metrics/summary")
    def tenant_metrics(slug: str, days: int = 7, session: Session = Depends(db)):
        biz = get_business(session, slug)
        return _tenant_summary(session, biz, datetime.utcnow() - timedelta(days=days))

    @app.get("/owner/{slug}/metrics/events")
    def tenant_events(slug: str, limit: int = 200, kind: CallEventKind | None = None,
                      session: Session = Depends(db)):
        biz = get_business(session, slug)
        stmt = select(CallEvent).where(CallEvent.business_id == biz.id)
        if kind:
            stmt = stmt.where(CallEvent.kind == kind)
        rows = session.exec(stmt.order_by(CallEvent.created_at.desc()).limit(limit)).all()
        return rows

    @app.get("/admin/metrics/summary")
    def admin_metrics(days: int = 7, session: Session = Depends(db)):
        since = datetime.utcnow() - timedelta(days=days)
        tenants = session.exec(select(Business)).all()
        return {"days": days, "tenants": [_tenant_summary(session, b, since) for b in tenants]}
