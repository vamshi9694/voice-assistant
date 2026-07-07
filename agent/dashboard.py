"""Owner dashboard — served on the PUBLIC agent app, proxied to the control plane.

The control plane (FastAPI, port 8080) is loopback-only in production, so the
browser can't hit its /owner/* endpoints directly. This module registers, on
the same public FastAPI app that serves /monitor:

  GET /dashboard              -> the dashboard HTML page
  ANY /dashboard/api/{path}   -> proxied to  http://127.0.0.1:8080/owner/{path}

So the page talks only to the public app, and this module relays to the
control plane over loopback. Optional DASHBOARD_TOKEN gates access (?token=).
"""
from __future__ import annotations

import os
import pathlib

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from pipecat.runner.run import app

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")


def _check_token(request: "Request") -> None:
    if DASHBOARD_TOKEN and request.query_params.get("token") != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="dashboard token required")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> str:
    _check_token(request)
    return _PAGE


@app.api_route("/dashboard/api/{path:path}", methods=["GET", "POST"])
async def dashboard_proxy(path: str, request: Request) -> JSONResponse:
    """Relay dashboard calls to the control plane's /owner/* endpoints."""
    _check_token(request)
    url = f"{CONTROL_PLANE}/owner/{path}"
    # Forward query params except our own token.
    params = {k: v for k, v in request.query_params.items() if k != "token"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if request.method == "POST":
                body = await request.body()
                r = await client.post(url, params=params, content=body,
                                      headers={"content-type": "application/json"})
            else:
                r = await client.get(url, params=params)
        # Pass through JSON + status.
        try:
            return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception:
            return JSONResponse(status_code=r.status_code, content={"error": r.text[:200]})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": f"control plane unreachable: {type(e).__name__}"})


_PAGE = (pathlib.Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
