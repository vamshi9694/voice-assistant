"""Website crawler -> CrawlDraft -> human approval -> KB / business profile.

Safety properties:
  - Crawls ONLY the tenant's approved website domain (subdomains allowed);
    external links are never followed.
  - Respects robots.txt Disallow rules for User-agent: *.
  - Hard caps: MAX_PAGES pages, MAX_DEPTH link depth, 15s/page timeout.
  - NOTHING publishes without approval; every fact keeps its source URL, and
    published KB entries carry source_url + updated_at (last-verified date).

Extraction: LLM (OPENAI_API_KEY) when available, else heuristics for
address / hours / FAQs / policies / menu links.
"""
import json
import os
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .ingest import _same_domain, html_to_text
from .models import Business, CrawlDraft, DraftStatus, KBEntry

MAX_PAGES = int(os.getenv("CRAWL_MAX_PAGES", "10"))
MAX_DEPTH = int(os.getenv("CRAWL_MAX_DEPTH", "2"))
PAGE_TIMEOUT = 15
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_INGEST_MODEL", "gpt-4o-mini")

DAY_RE = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|daily|weekday|weekend)\b",
    re.I,
)
TIME_RE = re.compile(r"\b\d{1,2}([:.]\d{2})?\s*(am|pm)\b|\b\d{1,2}[:.]\d{2}\b", re.I)
ADDR_RE = re.compile(
    r"\d+[\w\-/]*\s+[\w'’.\- ]{2,40}\b(st|street|rd|road|ave|avenue|blvd|boulevard|ln|lane|dr|drive|hwy|highway|pde|parade|tce|terrace|way|place|pl|court|ct)\b",
    re.I,
)
POLICY_WORDS = re.compile(r"\b(reservation|booking|book|cancellation|deposit|walk[- ]?in|byo|corkage|large group|private|no[- ]show)\b", re.I)
MENU_LINK = re.compile(r"menu", re.I)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (href, anchor-ish text)
        self._href = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(t for t in self._buf if t)))
            self._href = None


def _robots_disallows(client: httpx.Client, base: str) -> list[str]:
    try:
        r = client.get(urljoin(base, "/robots.txt"), timeout=5)
        if r.status_code != 200:
            return []
        rules, active = [], False
        for line in r.text.splitlines():
            line = line.split("#")[0].strip()
            if line.lower().startswith("user-agent:"):
                active = line.split(":", 1)[1].strip() == "*"
            elif active and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    rules.append(path)
        return rules
    except Exception:  # noqa: BLE001
        return []


def _allowed(url: str, disallows: list[str]) -> bool:
    path = urlparse(url).path or "/"
    return not any(path.startswith(rule) for rule in disallows)


def crawl_site(start_url: str, approved_website: str) -> tuple[list[dict], list[str], int]:
    """BFS within the approved domain. Returns (pages, menu_urls, count);
    pages = [{"url", "text"}]."""
    pages, menu_urls, seen = [], [], set()
    queue: list[tuple[str, int]] = [(start_url, 0)]
    with httpx.Client(timeout=PAGE_TIMEOUT, follow_redirects=True, trust_env=False,
                      headers={"User-Agent": "ReceptionistBot/1.0 (+site-owner approved)"}) as client:
        disallows = _robots_disallows(client, start_url)
        while queue and len(pages) < MAX_PAGES:
            url, depth = queue.pop(0)
            clean = url.split("#")[0].rstrip("/") or url
            if clean in seen or not _same_domain(url, approved_website) or not _allowed(url, disallows):
                continue
            seen.add(clean)
            try:
                r = client.get(url)
                if r.status_code != 200 or "text/html" not in r.headers.get("content-type", "text/html"):
                    continue
            except Exception:  # noqa: BLE001
                continue
            pages.append({"url": str(r.url), "text": html_to_text(r.text)[:20000]})
            lp = _LinkParser()
            lp.feed(r.text)
            for href, anchor in lp.links:
                absu = urljoin(str(r.url), href)
                if not _same_domain(absu, approved_website):
                    continue
                if MENU_LINK.search(href) or MENU_LINK.search(anchor or ""):
                    if absu not in menu_urls:
                        menu_urls.append(absu)
                if depth + 1 <= MAX_DEPTH:
                    queue.append((absu, depth + 1))
    return pages, menu_urls, len(pages)


# ------------------------------ extraction ------------------------------

def _heuristic_facts(pages: list[dict]) -> tuple[list[dict], str]:
    facts, seen_vals = [], set()

    def add(ftype, topic, value, url):
        key = (ftype, value.lower())
        if value and key not in seen_vals:
            seen_vals.add(key)
            facts.append({"type": ftype, "topic": topic, "value": value[:500], "source_url": url})

    for p in pages:
        lines = [" ".join(l.split()) for l in p["text"].splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if len(line) > 300:
                continue
            if ADDR_RE.search(line) and len(line) < 120:
                add("address", "address", line, p["url"])
            if DAY_RE.search(line) and TIME_RE.search(line):
                add("hours", "opening hours", line, p["url"])
            if line.endswith("?") and 10 < len(line) < 150 and i + 1 < len(lines):
                answer = lines[i + 1]
                if 10 < len(answer) < 400 and not answer.endswith("?"):
                    add("faq", line.rstrip("?"), answer, p["url"])
            elif POLICY_WORDS.search(line) and 20 < len(line) < 300 and not line.endswith("?"):
                add("policy", "reservation policy", line, p["url"])
    notes = "heuristic extraction — verify every fact before approving"
    return facts, notes


def _llm_facts(pages: list[dict]) -> tuple[list[dict], str]:
    corpus = "\n\n".join(f"[PAGE {p['url']}]\n{p['text'][:6000]}" for p in pages)[:30000]
    prompt = (
        "From this restaurant website text, extract facts as JSON "
        '{"facts":[{"type":"address"|"hours"|"faq"|"policy","topic":str,"value":str,"source_url":str}]}. '
        "type=address: the street address. type=hours: opening hours statements. "
        "type=faq: question (topic) + answer (value) pairs useful to a phone receptionist "
        "(parking, dietary, byo, kids, gift cards...). type=policy: reservation/booking/"
        "cancellation/large-group policies. source_url = the [PAGE ...] the fact came from. "
        "NEVER invent facts.\n\n" + corpus
    )
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        json={"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": prompt}], "temperature": 0},
        timeout=90,
    )
    r.raise_for_status()
    data = json.loads(r.json()["choices"][0]["message"]["content"])
    return data.get("facts", []), "extracted by LLM — verify before approving"


def extract_facts(pages: list[dict]) -> tuple[list[dict], str]:
    if OPENAI_KEY:
        try:
            return _llm_facts(pages)
        except Exception as e:  # noqa: BLE001
            facts, notes = _heuristic_facts(pages)
            return facts, f"LLM failed ({type(e).__name__}); {notes}"
    return _heuristic_facts(pages)


# ------------------------------ routes ------------------------------

def wire(app, db, get_business):

    class CrawlStart(BaseModel):
        start_url: str | None = None   # defaults to the tenant's website

    @app.post("/owner/{slug}/crawl")
    def start_crawl(slug: str, body: CrawlStart, session: Session = Depends(db)):
        biz = get_business(session, slug)
        if not biz.website:
            raise HTTPException(422, "set the business website first — it is the approved crawl domain")
        start = body.start_url or biz.website
        if "://" not in start:
            start = "https://" + start
        if not _same_domain(start, biz.website):
            raise HTTPException(403, f"start_url must be on the approved domain ({biz.website})")

        pages, menu_urls, n = crawl_site(start, biz.website)
        if not pages:
            raise HTTPException(502, "could not fetch any pages from the site")
        facts, notes = extract_facts(pages)
        draft = CrawlDraft(
            business_id=biz.id, start_url=start, pages_crawled=n,
            facts_json=json.dumps(facts), menu_urls_json=json.dumps(menu_urls),
            extraction_notes=notes,
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        return {
            "draft_id": draft.id, "pages_crawled": n,
            "facts_found": len(facts), "facts": facts,
            "menu_urls": menu_urls, "notes": notes,
            "next": f"review then POST /owner/{slug}/crawl/drafts/{draft.id}/approve",
        }

    @app.get("/owner/{slug}/crawl/drafts")
    def list_crawl_drafts(slug: str, session: Session = Depends(db)):
        biz = get_business(session, slug)
        rows = session.exec(
            select(CrawlDraft).where(CrawlDraft.business_id == biz.id)
            .order_by(CrawlDraft.created_at.desc())
        ).all()
        out = []
        for d in rows:
            v = d.model_dump()
            v["facts"] = json.loads(d.facts_json or "[]")
            v["menu_urls"] = json.loads(d.menu_urls_json or "[]")
            del v["facts_json"], v["menu_urls_json"]
            out.append(v)
        return out

    class CrawlApprove(BaseModel):
        # Facts to publish — usually the reviewed/edited subset of draft facts.
        # Omit to publish ALL facts from the draft.
        facts: list[dict] | None = None

    @app.post("/owner/{slug}/crawl/drafts/{draft_id}/approve")
    def approve_crawl(slug: str, draft_id: int, body: CrawlApprove, session: Session = Depends(db)):
        biz = get_business(session, slug)
        draft = session.get(CrawlDraft, draft_id)
        if not draft or draft.business_id != biz.id:
            raise HTTPException(404, "draft not found")
        if draft.status != DraftStatus.pending:
            raise HTTPException(409, f"draft already {draft.status}")
        facts = body.facts if body.facts is not None else json.loads(draft.facts_json or "[]")
        if not facts:
            raise HTTPException(422, "no facts to publish")

        published = {"address": 0, "kb": 0}
        for f in facts:
            ftype = f.get("type", "faq")
            value = str(f.get("value", "")).strip()
            src = str(f.get("source_url", draft.start_url))[:500]
            if not value:
                continue
            if ftype == "address":
                biz.address = value[:200]
                session.add(biz)
                published["address"] += 1
                continue
            # hours / faq / policy -> KB entries (structured ServicePeriod rows
            # stay human-managed; spoken hours facts still help the agent)
            topic = str(f.get("topic") or ftype)[:100]
            row = session.exec(select(KBEntry).where(
                KBEntry.business_id == biz.id, KBEntry.topic == topic)).first()
            if row:
                row.answer, row.source_url, row.updated_at = value, src, datetime.utcnow()
            else:
                row = KBEntry(business_id=biz.id, topic=topic, answer=value, source_url=src)
            session.add(row)
            published["kb"] += 1

        draft.status = DraftStatus.approved
        draft.reviewed_at = datetime.utcnow()
        session.add(draft)
        session.commit()
        return {"ok": True, **published}

    @app.post("/owner/{slug}/crawl/drafts/{draft_id}/reject")
    def reject_crawl(slug: str, draft_id: int, session: Session = Depends(db)):
        biz = get_business(session, slug)
        draft = session.get(CrawlDraft, draft_id)
        if not draft or draft.business_id != biz.id:
            raise HTTPException(404, "draft not found")
        draft.status = DraftStatus.rejected
        draft.reviewed_at = datetime.utcnow()
        session.add(draft)
        session.commit()
        return {"ok": True}
