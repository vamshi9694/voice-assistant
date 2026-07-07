"""End-to-end verification of the multi-tenant control plane.

    python verify.py

Runs in-process (FastAPI TestClient) against a throwaway SQLite DB — safe to
run anywhere, no ports, no external services. Covers: tenant routing, auth +
RBAC, config editing, reservations/orders safety rules, idempotency, menu
ingestion + approval, website crawl + approval, vector KB isolation, metrics.
"""
import io
import json
import os
import tempfile
import threading

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkstemp(suffix='.db')[1]}"
os.environ.pop("OPENAI_API_KEY", None)   # force offline extraction paths

from fastapi.testclient import TestClient  # noqa: E402

import seed  # noqa: E402
from api.main import app  # noqa: E402

seed.seed()
c = TestClient(app)

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {extra}")


def login(email, pw):
    r = c.post("/auth/login", json={"email": email, "password": pw})
    return r.json().get("token")


admin = {"Authorization": f"Bearer {login('admin@platform.local', 'admin123')}"}
tacos = {"Authorization": f"Bearer {login('owner@tacos.local', 'owner123')}"}
luigi = {"Authorization": f"Bearer {login('owner@luigis.local', 'owner123')}"}

print("— tenant routing")
check("resolve tacos number", c.get("/agent/resolve?to=%2B61370000002").json().get("slug") == "tacos-el-rey")
check("resolve luigis number", c.get("/agent/resolve?to=%2B61370000001").json().get("slug") == "luigis-carlton")
check("unknown number 404", c.get("/agent/resolve?to=%2B10000000000").status_code == 404)
c.post("/admin/tenants/tacos-el-rey/status", json={"status": "paused"}, headers=admin)
check("paused tenant 423", c.get("/agent/resolve?to=%2B61370000002").status_code == 423)
c.post("/admin/tenants/tacos-el-rey/status", json={"status": "active"}, headers=admin)

print("— auth / RBAC")
check("no token -> 401", c.get("/admin/tenants").status_code == 401)
check("bad password -> 401", c.post("/auth/login", json={"email": "owner@tacos.local", "password": "x"}).status_code == 401)
check("tenant blocked from /admin", c.get("/admin/tenants", headers=tacos).status_code == 403)
check("tenant blocked from other tenant", c.get("/owner/luigis-carlton/business", headers=tacos).status_code == 403)
check("tenant reads own config", c.get("/owner/tacos-el-rey/business", headers=tacos).status_code == 200)
check("admin reads any tenant", c.get("/owner/luigis-carlton/business", headers=admin).status_code == 200)
check("slug not client-editable", c.patch("/owner/tacos-el-rey/business", json={"slug": "hax"}, headers=tacos).status_code == 422)
check("config patch works", c.patch("/owner/tacos-el-rey/business", json={"max_party_size": 6}, headers=tacos).status_code == 200)

print("— reservations safety")
body = {"date": "2026-07-10", "time": "19:00", "party_size": 4, "guest_name": "Ana",
        "guest_phone": "0412345678", "idempotency_key": "vk1"}
r1 = c.post("/agent/tacos-el-rey/reservations", json=body).json()
r2 = c.post("/agent/tacos-el-rey/reservations", json=body).json()
check("reservation created", r1.get("created") is True)
check("idempotent replay", r2.get("idempotent_replay") is True and r2["reservation_id"] == r1["reservation_id"])
check("10-digit phone enforced", c.post("/agent/tacos-el-rey/reservations",
      json={**body, "guest_phone": "123", "idempotency_key": "vk2"}).status_code == 422)
big = c.post("/agent/tacos-el-rey/reservations", json={**body, "party_size": 9, "idempotency_key": "vk3"}).json()
check("large party blocked server-side", big.get("created") is False)
check("cross-tenant reservations invisible", c.get("/owner/luigis-carlton/reservations", headers=luigi).json() == [])

print("— orders safety")
bad = c.post("/agent/tacos-el-rey/orders", json={"guest_name": "A", "guest_phone": "0412345678",
      "items": [{"name": "Invented dish", "qty": 1}]}).json()
check("invented item rejected", bad.get("created") is False and "Invented dish" in bad.get("unknown_items", []))
ok = c.post("/agent/tacos-el-rey/orders", json={"guest_name": "A", "guest_phone": "0412345678",
     "items": [{"name": "al pastor TACO", "qty": 2}], "idempotency_key": "vo1"}).json()
check("order created, price from DB", ok.get("created") is True and ok.get("total") == 13.0)
off = c.post("/agent/luigis-carlton/orders", json={"guest_name": "B", "guest_phone": "0412345678",
      "items": [{"name": "Tiramisu", "qty": 1}]}).json()
check("orders-disabled tenant refuses", off.get("created") is False)

print("— menu ingestion + approval")
csv_bytes = b"section,name,price\nTest,Verify taco,5.0\n"
d = c.post("/owner/tacos-el-rey/menu/ingest/csv", headers=tacos,
           files={"file": ("m.csv", io.BytesIO(csv_bytes), "text/csv")}).json()
check("csv draft created", d.get("items_found") == 1)
pre = json.dumps(c.get("/agent/tacos-el-rey/menu").json())
check("draft not live before approval", "Verify taco" not in pre)
ap = c.post(f"/owner/tacos-el-rey/menu/drafts/{d['draft_id']}/approve", json={"mode": "merge"}, headers=tacos).json()
check("approve publishes", ap.get("ok") and "Verify taco" in json.dumps(c.get("/agent/tacos-el-rey/menu").json()))
check("double approve 409", c.post(f"/owner/tacos-el-rey/menu/drafts/{d['draft_id']}/approve",
      json={}, headers=tacos).status_code == 409)
check("url ingest blocks foreign domain", c.post("/owner/tacos-el-rey/menu/ingest/url",
      json={"url": "http://evil.com/menu"}, headers=tacos).status_code == 403)

print("— website crawl + approval")
import http.server, functools
site = tempfile.mkdtemp()
open(f"{site}/index.html", "w").write(
    "<html><body><p>Open Monday to Friday 9am - 5pm</p>"
    "<p>Do you cater?</p><p>Yes, we cater for events of any size with notice.</p></body></html>")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
      functools.partial(http.server.SimpleHTTPRequestHandler, directory=site))
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
c.patch("/owner/tacos-el-rey/business", json={"website": f"http://127.0.0.1:{port}"}, headers=tacos)
cr = c.post("/owner/tacos-el-rey/crawl", json={}, headers=tacos).json()
check("crawl finds facts", cr.get("facts_found", 0) >= 2, str(cr)[:120])
ca = c.post(f"/owner/tacos-el-rey/crawl/drafts/{cr['draft_id']}/approve", json={}, headers=tacos).json()
check("crawl facts publish to KB", ca.get("ok") and ca.get("kb", 0) >= 1)
kb = c.get("/owner/tacos-el-rey/kb", headers=tacos).json()
check("KB entry has source_url", any(k["source_url"] for k in kb))
srv.shutdown()

print("— vector KB isolation")
c.post("/owner/tacos-el-rey/kb/notes", json={"title": "Wifi", "content": "wifi password SALSAVERDE"}, headers=tacos)
c.post("/owner/luigis-carlton/kb/notes", json={"title": "Wifi", "content": "wifi password TRATTORIA123"}, headers=luigi)
rt = c.post("/agent/tacos-el-rey/kb/search", json={"query": "wifi password"}).json()["results"]
rl = c.post("/agent/luigis-carlton/kb/search", json={"query": "wifi password"}).json()["results"]
check("tacos sees only its secret", rt and "SALSAVERDE" in rt[0]["text"] and "TRATTORIA" not in json.dumps(rt))
check("luigis sees only its secret", rl and "TRATTORIA123" in rl[0]["text"] and "SALSAVERDE" not in json.dumps(rl))
check("kb sync ok", c.post("/owner/tacos-el-rey/kb/sync", headers=tacos).json().get("ok") is True)

print("— metrics")
c.post("/agent/tacos-el-rey/metrics", json=[{"call_id": "V1", "kind": "llm_ttfb", "value_ms": 500},
                                            {"call_id": "V1", "kind": "dead_air", "value_ms": 3000}])
s = c.get("/admin/metrics/summary", headers=admin).json()
t = next(x for x in s["tenants"] if x["slug"] == "tacos-el-rey")
check("summary aggregates latency", t["avg_llm_ms"] == 500.0)
check("summary counts dead air", t["dead_air"] == 1)
check("duplicate auto-logged earlier", t["duplicates"] >= 1)
check("tenant blocked from admin metrics", c.get("/admin/metrics/summary", headers=tacos).status_code == 403)

print("— dashboards")
check("client SPA served", c.get("/app").status_code == 200)
check("admin SPA served", c.get("/admin-ui").status_code == 200)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
