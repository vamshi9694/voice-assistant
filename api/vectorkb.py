"""Per-tenant knowledge base with vector search.

Contents: custom notes, uploaded documents (txt/md/pdf), and synced snapshots
of structured data (approved website facts + FAQs from KBEntry, menu facts).

Isolation: KBChunk carries business_id and EVERY query filters on it — there
is no cross-tenant code path. The search endpoint takes the tenant slug from
the URL, same as every other agent endpoint.

Embeddings: OpenAI text-embedding-3-small when OPENAI_API_KEY is set (vector
cosine search). Without a key, chunks are stored unembedded and search falls
back to lexical scoring — dev works offline, prod gets true semantic search.
Storage is a portable JSON float list; per-tenant KBs are small (hundreds of
chunks), so scoring in-process is <10ms. Swap to pgvector later behind
search() if a tenant ever outgrows this.
"""
import io
import json
import math
import os
import re
from datetime import datetime

import httpx
from fastapi import Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from .models import KBChunk, KBDocument, KBEntry, MenuItem

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
CHUNK_CHARS = 800
CHUNK_OVERLAP = 120
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# ------------------------------ chunking ------------------------------

def chunk_text(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            # prefer to break at a paragraph/sentence boundary
            for sep in ("\n\n", ". ", "\n"):
                cut = text.rfind(sep, start + CHUNK_CHARS // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        chunks.append(text[start:end].strip())
        start = max(end - CHUNK_OVERLAP, start + 1)
        if end == len(text):
            break
    return [c for c in chunks if c]


# ------------------------------ embeddings ------------------------------

def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """None = no embedding available (lexical fallback mode)."""
    if not OPENAI_KEY or not texts:
        return None
    try:
        r = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": EMBED_MODEL, "input": texts[:256]},
            timeout=60,
        )
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]
    except Exception:  # noqa: BLE001 — degrade to lexical, never 500
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


_WORD = re.compile(r"[a-zà-ÿ0-9]+", re.I)
_STOP = {
    # en
    "a", "an", "and", "are", "at", "be", "can", "do", "does", "for", "from",
    "have", "how", "i", "in", "is", "it", "me", "much", "my", "of", "on", "or",
    "the", "there", "to", "we", "what", "when", "where", "which", "who", "why",
    "with", "you", "your",
    # es
    "el", "la", "los", "las", "un", "una", "de", "del", "que", "es", "en",
    "por", "para", "con", "como", "cuanto", "donde", "hay",
}


def _lexical_score(query: str, text: str) -> float:
    q = {w for w in _WORD.findall(query.lower()) if w not in _STOP}
    if not q:
        return 0.0
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in q)
    # coverage bonus: chunks matching MORE DISTINCT query words rank higher
    distinct = len(q & set(words)) / len(q)
    return (hits / math.sqrt(len(words))) * (1 + distinct)


# ------------------------------ indexing ------------------------------

def index_document(session: Session, business_id: int, title: str, source: str,
                   content: str, source_url: str = "",
                   replace_doc_id: int | None = None) -> KBDocument:
    """Create/replace a document and its chunks (embedded when possible)."""
    if replace_doc_id:
        old = session.get(KBDocument, replace_doc_id)
        if old and old.business_id == business_id:
            for ch in session.exec(select(KBChunk).where(KBChunk.document_id == old.id)).all():
                session.delete(ch)
            session.delete(old)
            session.flush()

    doc = KBDocument(business_id=business_id, title=title[:200], source=source,
                     source_url=source_url[:500], content=content)
    session.add(doc)
    session.flush()

    chunks = chunk_text(content)
    vecs = embed_texts(chunks)
    for i, text in enumerate(chunks):
        session.add(KBChunk(
            business_id=business_id, document_id=doc.id, seq=i, text=text,
            embedding_json=json.dumps(vecs[i]) if vecs else "",
        ))
    session.commit()
    session.refresh(doc)
    return doc


def search_kb(session: Session, business_id: int, query: str, k: int = 4) -> list[dict]:
    """Tenant-scoped top-k. Vector cosine when both query and chunks have
    embeddings; lexical otherwise."""
    chunks = session.exec(
        select(KBChunk).where(KBChunk.business_id == business_id)
    ).all()
    if not chunks:
        return []

    qvecs = embed_texts([query])
    scored: list[tuple[float, KBChunk]] = []
    if qvecs:
        qv = qvecs[0]
        for ch in chunks:
            if ch.embedding_json:
                scored.append((_cosine(qv, json.loads(ch.embedding_json)), ch))
            else:
                scored.append((_lexical_score(query, ch.text), ch))
    else:
        scored = [(_lexical_score(query, ch.text), ch) for ch in chunks]

    scored.sort(key=lambda t: t[0], reverse=True)
    docs = {d.id: d for d in session.exec(
        select(KBDocument).where(KBDocument.business_id == business_id)).all()}
    out = []
    for score, ch in scored[:k]:
        if score <= 0:
            continue
        doc = docs.get(ch.document_id)
        out.append({
            "text": ch.text, "score": round(float(score), 4),
            "title": doc.title if doc else "", "source": doc.source if doc else "",
            "source_url": doc.source_url if doc else "",
        })
    return out


# ------------------------------ routes ------------------------------

def wire(app, db, get_business):

    # ---------- agent-facing ----------

    class SearchBody(BaseModel):
        query: str
        k: int = 4

    @app.post("/agent/{slug}/kb/search")
    def agent_kb_search(slug: str, body: SearchBody, session: Session = Depends(db)):
        biz = get_business(session, slug)
        results = search_kb(session, biz.id, body.query, max(1, min(body.k, 8)))
        return {"results": results}

    # ---------- owner-facing ----------

    class NoteBody(BaseModel):
        title: str
        content: str

    @app.post("/owner/{slug}/kb/notes")
    def add_note(slug: str, body: NoteBody, session: Session = Depends(db)):
        biz = get_business(session, slug)
        doc = index_document(session, biz.id, body.title, "note", body.content)
        return {"ok": True, "document_id": doc.id}

    @app.post("/owner/{slug}/kb/upload")
    async def upload_document(slug: str, file: UploadFile = File(...), session: Session = Depends(db)):
        biz = get_business(session, slug)
        blob = await file.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large (10MB max)")
        name = file.filename or "document"
        if name.lower().endswith(".pdf"):
            try:
                from pypdf import PdfReader
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)
            except Exception:
                raise HTTPException(422, "could not read PDF")
        else:
            try:
                text = blob.decode("utf-8", errors="replace")
            except Exception:
                raise HTTPException(422, "expected a text, markdown, or PDF file")
        if not text.strip():
            raise HTTPException(422, "no extractable text")
        doc = index_document(session, biz.id, name, "document", text)
        return {"ok": True, "document_id": doc.id, "chunks": len(chunk_text(text))}

    @app.get("/owner/{slug}/kb/documents")
    def list_documents(slug: str, session: Session = Depends(db)):
        biz = get_business(session, slug)
        rows = session.exec(
            select(KBDocument).where(KBDocument.business_id == biz.id)
            .order_by(KBDocument.updated_at.desc())
        ).all()
        return [{"id": d.id, "title": d.title, "source": d.source,
                 "source_url": d.source_url, "chars": len(d.content),
                 "updated_at": d.updated_at} for d in rows]

    @app.post("/owner/{slug}/kb/documents/{doc_id}/delete")
    def delete_document(slug: str, doc_id: int, session: Session = Depends(db)):
        biz = get_business(session, slug)
        doc = session.get(KBDocument, doc_id)
        if not doc or doc.business_id != biz.id:
            raise HTTPException(404, "document not found")
        for ch in session.exec(select(KBChunk).where(KBChunk.document_id == doc.id)).all():
            session.delete(ch)
        session.delete(doc)
        session.commit()
        return {"ok": True}

    @app.post("/owner/{slug}/kb/sync")
    def sync_structured(slug: str, session: Session = Depends(db)):
        """Re-index structured sources into the vector store: approved facts +
        FAQs (KBEntry, incl. crawler-published entries) and the live menu.
        Idempotent — replaces the previous synced snapshots."""
        biz = get_business(session, slug)

        def _replace(title: str):
            row = session.exec(select(KBDocument).where(
                KBDocument.business_id == biz.id, KBDocument.title == title,
                KBDocument.source.in_(["faq", "menu"]))).first()  # type: ignore[attr-defined]
            return row.id if row else None

        entries = session.exec(select(KBEntry).where(KBEntry.business_id == biz.id)).all()
        synced = {}
        if entries:
            content = "\n\n".join(f"{e.topic}: {e.answer}" for e in entries)
            doc = index_document(session, biz.id, "Approved facts & FAQs", "faq",
                                 content, replace_doc_id=_replace("Approved facts & FAQs"))
            synced["faqs"] = doc.id

        items = session.exec(select(MenuItem).where(
            MenuItem.business_id == biz.id, MenuItem.available == True)).all()  # noqa: E712
        if items:
            content = "\n".join(
                f"{m.section} — {m.name}: ${m.price:.2f}"
                + (f" ({m.dietary})" if m.dietary else "")
                + (f". {m.description}" if m.description else "")
                for m in items
            )
            doc = index_document(session, biz.id, "Menu facts", "menu",
                                 content, replace_doc_id=_replace("Menu facts"))
            synced["menu"] = doc.id
        return {"ok": True, "synced": synced}
