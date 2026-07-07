"""Menu + phone orders.

Safety rules enforced HERE (server-side), not just in the prompt:
  - orders validate every line item against the tenant's live menu — the
    agent cannot sell an item that doesn't exist or is 86'd;
  - prices come from the menu row, never from the LLM;
  - orders_enabled=False tenants get a policy refusal the agent can speak;
  - idempotency_key dedupes retried create_order calls;
  - a valid callback phone is required.
"""
import json
from datetime import datetime

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from . import notify
from .idempotency import idem_get, idem_put
from .models import MenuItem, Order


def wire(app, db, get_business, require_callback_phone):

    # ------------------------- agent-facing -------------------------

    @app.get("/agent/{slug}/menu")
    def agent_menu(slug: str, session: Session = Depends(db)):
        """Compact available-items menu, grouped by section. Also injected
        into the system prompt at call start (see /agent/{slug}/context)."""
        biz = get_business(session, slug)
        rows = session.exec(
            select(MenuItem).where(MenuItem.business_id == biz.id, MenuItem.available == True)  # noqa: E712
        ).all()
        sections: dict[str, list] = {}
        for r in rows:
            sections.setdefault(r.section, []).append(
                {"name": r.name, "price": r.price, "description": r.description, "dietary": r.dietary}
            )
        return {"sections": sections, "orders_enabled": biz.orders_enabled}

    class OrderLine(BaseModel):
        name: str
        qty: int = 1
        notes: str = ""

    class OrderCreate(BaseModel):
        guest_name: str
        guest_phone: str
        items: list[OrderLine]
        notes: str = ""
        call_id: str | None = None
        idempotency_key: str | None = None

    @app.post("/agent/{slug}/orders")
    def create_order(slug: str, o: OrderCreate, session: Session = Depends(db)):
        biz = get_business(session, slug)
        cached = idem_get(session, biz.id, o.idempotency_key, "orders")
        if cached is not None:
            return cached
        if not biz.orders_enabled:
            return {"created": False,
                    "reason": "phone orders are not accepted here; offer to take a message instead"}
        o.guest_phone = require_callback_phone(o.guest_phone)
        if not o.items:
            return {"created": False, "reason": "order has no items"}

        # Validate EVERY line against the live menu (case-insensitive).
        menu = session.exec(
            select(MenuItem).where(MenuItem.business_id == biz.id, MenuItem.available == True)  # noqa: E712
        ).all()
        by_name = {m.name.lower().strip(): m for m in menu}
        unknown, lines, total = [], [], 0.0
        for line in o.items:
            m = by_name.get(line.name.lower().strip())
            if not m:
                unknown.append(line.name)
                continue
            qty = max(1, min(line.qty, 50))
            lines.append({"name": m.name, "qty": qty, "price": m.price, "notes": line.notes})
            total += m.price * qty
        if unknown:
            resp = {
                "created": False,
                "unknown_items": unknown,
                "reason": "these items are not on the menu — read the menu to the caller and correct the order",
            }
            return resp  # not stored: the agent should retry with fixed items

        row = Order(
            business_id=biz.id, guest_name=o.guest_name, guest_phone=o.guest_phone,
            items_json=json.dumps(lines), total=round(total, 2),
            pickup_minutes=biz.order_pickup_minutes, notes=o.notes, call_id=o.call_id,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        resp = {
            "created": True, "order_id": row.id, "total": row.total,
            "pickup_minutes": row.pickup_minutes,
            "items": lines,
        }
        idem_put(session, biz.id, o.idempotency_key, "orders", resp)
        session.commit()
        notify.notify_order(biz, row)
        return resp

    # ------------------------- owner-facing -------------------------

    class MenuItemUpsert(BaseModel):
        id: int | None = None
        section: str = "Mains"
        name: str
        description: str = ""
        price: float = 0.0
        dietary: str = ""
        available: bool = True

    @app.get("/owner/{slug}/menu")
    def list_menu(slug: str, session: Session = Depends(db)):
        biz = get_business(session, slug)
        rows = session.exec(select(MenuItem).where(MenuItem.business_id == biz.id)).all()
        return sorted((r.model_dump() for r in rows), key=lambda r: (r["section"], r["name"]))

    @app.post("/owner/{slug}/menu")
    def upsert_menu_item(slug: str, item: MenuItemUpsert, session: Session = Depends(db)):
        biz = get_business(session, slug)
        if item.id:
            row = session.get(MenuItem, item.id)
            if not row or row.business_id != biz.id:
                raise HTTPException(404, "menu item not found")
            for k, v in item.model_dump(exclude={"id"}).items():
                setattr(row, k, v)
            row.updated_at = datetime.utcnow()
        else:
            row = MenuItem(business_id=biz.id, **item.model_dump(exclude={"id"}))
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"ok": True, "id": row.id}

    @app.post("/owner/{slug}/menu/{item_id}/delete")
    def delete_menu_item(slug: str, item_id: int, session: Session = Depends(db)):
        biz = get_business(session, slug)
        row = session.get(MenuItem, item_id)
        if not row or row.business_id != biz.id:
            raise HTTPException(404, "menu item not found")
        session.delete(row)
        session.commit()
        return {"ok": True}

    @app.get("/owner/{slug}/orders")
    def list_orders(slug: str, limit: int = 100, session: Session = Depends(db)):
        biz = get_business(session, slug)
        rows = session.exec(
            select(Order).where(Order.business_id == biz.id)
            .order_by(Order.created_at.desc()).limit(limit)
        ).all()
        out = []
        for r in rows:
            d = r.model_dump()
            d["items"] = json.loads(r.items_json or "[]")
            out.append(d)
        return out

    class OrderStatusUpdate(BaseModel):
        status: str

    @app.post("/owner/{slug}/orders/{order_id}/status")
    def set_order_status(slug: str, order_id: int, u: OrderStatusUpdate, session: Session = Depends(db)):
        biz = get_business(session, slug)
        row = session.get(Order, order_id)
        if not row or row.business_id != biz.id:
            raise HTTPException(404, "order not found")
        row.status = u.status
        session.add(row)
        session.commit()
        return {"ok": True}
