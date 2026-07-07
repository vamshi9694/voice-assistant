"""Menu ingestion: CSV / PDF / image / website URL -> MenuDraft -> approve -> live menu.

Safety properties:
  - NOTHING ingested goes live without explicit approval (MenuDraft.status).
  - URL ingestion only fetches from the tenant's approved website domain.
  - Provenance (source + source_url) is stamped on every published item.
  - Extraction prefers an LLM when OPENAI_API_KEY is set; otherwise a
    price-pattern heuristic (CSV never needs either).

CSV format (header required, extra columns ignored):
    section,name,description,price,dietary
"""
import csv
import io
import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from fastapi import Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from .models import DraftStatus, MenuDraft, MenuItem

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_INGEST_MODEL", "gpt-4o-mini")

# ------------------------------ extraction ------------------------------

PRICE_RE = re.compile(r"[\$€£]?\s?(\d{1,3}(?:[.,]\d{2})?)\s*$")


def _heuristic_extract(text: str) -> tuple[list[dict], str]:
    """No-LLM fallback: 'Dish name ... 24.50' lines become items; short
    price-less lines become section headers."""
    items, section, notes = [], "Menu", []
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip(" .-·…")
        if not line or len(line) > 120:
            continue
        m = PRICE_RE.search(line)
        if m:
            name = line[: m.start()].strip(" .-·…$")
            if 2 <= len(name) <= 80:
                items.append({
                    "section": section, "name": name, "description": "",
                    "price": float(m.group(1).replace(",", ".")), "dietary": "",
                })
        elif len(line) <= 40 and not any(ch.isdigit() for ch in line):
            section = line.title() if line.isupper() else line
    if not items:
        notes.append("heuristic parser found no priced lines — review the source")
    return items, "; ".join(notes)


def _llm_extract(text: str) -> tuple[list[dict], str]:
    """LLM extraction via OpenAI chat completions (no SDK dep)."""
    prompt = (
        "Extract every menu item from this text. Return ONLY a JSON object "
        '{"items":[{"section":str,"name":str,"description":str,"price":number,"dietary":str}]}. '
        "dietary = comma-separated tags like GF, V, VG if stated, else empty. "
        "Do not invent items or prices; skip anything unclear.\n\n" + text[:24000]
    )
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": prompt}], "temperature": 0},
        timeout=60,
    )
    r.raise_for_status()
    data = json.loads(r.json()["choices"][0]["message"]["content"])
    return data.get("items", []), "extracted by LLM — verify prices before approving"


def _llm_extract_image(image_bytes: bytes, mime: str) -> tuple[list[dict], str]:
    import base64
    b64 = base64.b64encode(image_bytes).decode()
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": [
                  {"type": "text", "text":
                   'Extract every menu item from this menu photo. Return ONLY JSON '
                   '{"items":[{"section":str,"name":str,"description":str,"price":number,"dietary":str}]}. '
                   "Skip anything unreadable; never guess prices."},
                  {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
              ]}], "temperature": 0},
        timeout=120,
    )
    r.raise_for_status()
    data = json.loads(r.json()["choices"][0]["message"]["content"])
    return data.get("items", []), "extracted from image by LLM — verify before approving"


def _extract_from_text(text: str) -> tuple[list[dict], str]:
    if OPENAI_KEY:
        try:
            return _llm_extract(text)
        except Exception as e:  # noqa: BLE001 — fall back, never 500
            items, notes = _heuristic_extract(text)
            return items, f"LLM extraction failed ({type(e).__name__}); heuristic fallback. {notes}"
    return _heuristic_extract(text)


def _clean_items(items: list[dict]) -> list[dict]:
    out = []
    for i in items:
        name = str(i.get("name", "")).strip()
        if not name:
            continue
        try:
            price = round(float(i.get("price", 0) or 0), 2)
        except (TypeError, ValueError):
            price = 0.0
        out.append({
            "section": str(i.get("section", "Menu")).strip() or "Menu",
            "name": name[:100],
            "description": str(i.get("description", "")).strip()[:300],
            "price": price,
            "dietary": str(i.get("dietary", "")).strip()[:60],
        })
    return out


class _TextHTML(HTMLParser):
    """HTML -> visible text (stdlib only). Skips script/style/nav/footer."""
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _TextHTML()
    p.feed(html)
    return "\n".join(p.parts)


def _same_domain(url: str, approved_website: str) -> bool:
    """URL host must equal, or be a subdomain of, the tenant's website host."""
    if not approved_website:
        return False
    host = (urlparse(url).hostname or "").lower()
    approved = (urlparse(
        approved_website if "://" in approved_website else f"https://{approved_website}"
    ).hostname or "").lower()
    if not host or not approved:
        return False
    approved = approved.removeprefix("www.")
    host = host.removeprefix("www.")
    return host == approved or host.endswith("." + approved)


# ------------------------------ routes ------------------------------

def wire(app, db, get_business):

    def _make_draft(session: Session, biz, source: str, source_url: str,
                    items: list[dict], notes: str) -> dict:
        items = _clean_items(items)
        draft = MenuDraft(
            business_id=biz.id, source=source, source_url=source_url,
            items_json=json.dumps(items), extraction_notes=notes,
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        return {"draft_id": draft.id, "items_found": len(items),
                "items": items, "notes": notes,
                "next": f"review then POST /owner/{biz.slug}/menu/drafts/{draft.id}/approve"}

    async def _read_upload(file: UploadFile) -> bytes:
        blob = await file.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large (10MB max)")
        return blob

    @app.post("/owner/{slug}/menu/ingest/csv")
    async def ingest_csv(slug: str, file: UploadFile = File(...), session: Session = Depends(db)):
        biz = get_business(session, slug)
        blob = await _read_upload(file)
        try:
            rows = list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))))
        except Exception:
            raise HTTPException(422, "could not parse CSV (expect header: section,name,description,price,dietary)")
        cols = {c.lower().strip(): c for c in (rows[0].keys() if rows else [])}
        if "name" not in cols:
            raise HTTPException(422, "CSV needs at least a 'name' column (plus optional section, description, price, dietary)")
        items = [{
            "section": r.get(cols.get("section", ""), "Menu") or "Menu",
            "name": r.get(cols["name"], ""),
            "description": r.get(cols.get("description", ""), "") or "",
            "price": r.get(cols.get("price", ""), 0) or 0,
            "dietary": r.get(cols.get("dietary", ""), "") or "",
        } for r in rows]
        return _make_draft(session, biz, "csv", file.filename or "upload.csv", items, "")

    @app.post("/owner/{slug}/menu/ingest/pdf")
    async def ingest_pdf(slug: str, file: UploadFile = File(...), session: Session = Depends(db)):
        biz = get_business(session, slug)
        blob = await _read_upload(file)
        try:
            from pypdf import PdfReader
        except ImportError:
            raise HTTPException(501, "PDF ingestion needs pypdf (pip install pypdf)")
        try:
            reader = PdfReader(io.BytesIO(blob))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception:
            raise HTTPException(422, "could not read PDF")
        if not text.strip():
            raise HTTPException(422, "PDF has no extractable text (scanned image?) — try image ingestion")
        items, notes = _extract_from_text(text)
        return _make_draft(session, biz, "pdf", file.filename or "upload.pdf", items, notes)

    @app.post("/owner/{slug}/menu/ingest/image")
    async def ingest_image(slug: str, file: UploadFile = File(...), session: Session = Depends(db)):
        biz = get_business(session, slug)
        if not OPENAI_KEY:
            raise HTTPException(501, "image ingestion needs OPENAI_API_KEY (vision model)")
        blob = await _read_upload(file)
        mime = file.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            raise HTTPException(422, "not an image")
        try:
            items, notes = _llm_extract_image(blob, mime)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"vision extraction failed: {type(e).__name__}")
        return _make_draft(session, biz, "image", file.filename or "upload", items, notes)

    class UrlIngest(BaseModel):
        url: str

    @app.post("/owner/{slug}/menu/ingest/url")
    async def ingest_url(slug: str, body: UrlIngest, session: Session = Depends(db)):
        biz = get_business(session, slug)
        if not _same_domain(body.url, biz.website):
            approved = biz.website or "not set — save the business website first"
            raise HTTPException(403, f"URL must be on the approved website domain ({approved})")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
                r = await client.get(body.url)
                r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"could not fetch URL: {type(e).__name__}")
        text = html_to_text(r.text)
        if not text.strip():
            raise HTTPException(422, "page has no extractable text")
        items, notes = _extract_from_text(text)
        return _make_draft(session, biz, "website", body.url, items, notes)

    # ------------------------- draft review -------------------------

    @app.get("/owner/{slug}/menu/drafts")
    def list_drafts(slug: str, session: Session = Depends(db)):
        biz = get_business(session, slug)
        rows = session.exec(
            select(MenuDraft).where(MenuDraft.business_id == biz.id)
            .order_by(MenuDraft.created_at.desc())
        ).all()
        out = []
        for d in rows:
            v = d.model_dump()
            v["items"] = json.loads(d.items_json or "[]")
            del v["items_json"]
            out.append(v)
        return out

    class ApproveBody(BaseModel):
        mode: str = "merge"          # merge = upsert by name; replace = wipe menu first
        items: list[dict] | None = None   # optionally the edited item list from the review UI

    @app.post("/owner/{slug}/menu/drafts/{draft_id}/approve")
    def approve_draft(slug: str, draft_id: int, body: ApproveBody, session: Session = Depends(db)):
        biz = get_business(session, slug)
        draft = session.get(MenuDraft, draft_id)
        if not draft or draft.business_id != biz.id:
            raise HTTPException(404, "draft not found")
        if draft.status != DraftStatus.pending:
            raise HTTPException(409, f"draft already {draft.status}")
        items = _clean_items(body.items if body.items is not None
                             else json.loads(draft.items_json or "[]"))
        if not items:
            raise HTTPException(422, "no items to publish")

        if body.mode == "replace":
            for row in session.exec(select(MenuItem).where(MenuItem.business_id == biz.id)).all():
                session.delete(row)
            session.flush()

        existing = {m.name.lower(): m for m in session.exec(
            select(MenuItem).where(MenuItem.business_id == biz.id)).all()}
        created = updated = 0
        for i in items:
            row = existing.get(i["name"].lower())
            if row:
                row.section, row.description = i["section"], i["description"]
                row.price, row.dietary = i["price"], i["dietary"]
                row.source, row.source_url = draft.source, draft.source_url
                row.updated_at = datetime.utcnow()
                updated += 1
            else:
                session.add(MenuItem(business_id=biz.id, **i, source=draft.source,
                                     source_url=draft.source_url))
                created += 1
        draft.status = DraftStatus.approved
        draft.reviewed_at = datetime.utcnow()
        session.add(draft)
        session.commit()
        return {"ok": True, "created": created, "updated": updated, "mode": body.mode}

    @app.post("/owner/{slug}/menu/drafts/{draft_id}/reject")
    def reject_draft(slug: str, draft_id: int, session: Session = Depends(db)):
        biz = get_business(session, slug)
        draft = session.get(MenuDraft, draft_id)
        if not draft or draft.business_id != biz.id:
            raise HTTPException(404, "draft not found")
        draft.status = DraftStatus.rejected
        draft.reviewed_at = datetime.utcnow()
        session.add(draft)
        session.commit()
        return {"ok": True}
