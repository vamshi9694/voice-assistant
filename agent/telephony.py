"""Twilio voice webhook with tenant routing.

Pipecat's built-in `POST /` TwiML template doesn't forward the called number,
so the media stream can't tell which tenant was dialed. This module registers
`POST /voice` on the runner app:

  1. Twilio POSTs form fields (To, From, CallSid) when a call arrives.
  2. We resolve To -> tenant via the control plane (/agent/resolve).
     - unknown/paused number: speak a polite line and hang up (no stream).
  3. We return TwiML that opens the media stream AND passes to/from/slug as
     <Parameter> elements, which arrive in call_data["body"] in bot().

Point every tenant number's voice webhook at  https://$PUBLIC_HOST/voice
(set-twilio-webhook.sh does this).
"""
import os
from xml.sax.saxutils import escape

import httpx
from fastapi import Request, Response
from loguru import logger

from pipecat.runner.run import app

CONTROL_PLANE = os.getenv("CONTROL_PLANE_URL", "http://127.0.0.1:8080")


def _reject_twiml(line: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(line)}</Say><Hangup/></Response>'


@app.post("/voice")
async def voice_webhook(request: Request) -> Response:
    form = await request.form()
    to = str(form.get("To", ""))
    from_ = str(form.get("From", ""))
    call_sid = str(form.get("CallSid", ""))

    # Resolve tenant by called number. Dev fallback: BUSINESS_SLUG env.
    slug = ""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{CONTROL_PLANE}/agent/resolve", params={"to": to})
        if r.status_code == 200:
            slug = r.json()["slug"]
        elif r.status_code == 423:
            logger.warning(f"[{call_sid}] tenant paused for {to}")
            return Response(
                content=_reject_twiml("Sorry, we can't take calls right now. Please try again later."),
                media_type="application/xml",
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"[{call_sid}] resolve failed: {e}")

    if not slug:
        slug = os.getenv("BUSINESS_SLUG", "")
        if slug:
            logger.warning(f"[{call_sid}] no tenant for {to}; using BUSINESS_SLUG fallback '{slug}'")
        else:
            logger.error(f"[{call_sid}] no tenant for {to} and no fallback — rejecting")
            return Response(
                content=_reject_twiml("Sorry, this number isn't in service."),
                media_type="application/xml",
            )

    host = os.getenv("PUBLIC_HOST", request.url.hostname or "")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws">
      <Parameter name="to" value="{escape(to)}"/>
      <Parameter name="from" value="{escape(from_)}"/>
      <Parameter name="slug" value="{escape(slug)}"/>
    </Stream>
  </Connect>
</Response>"""
    logger.info(f"[{call_sid}] inbound {from_} -> {to} => tenant '{slug}'")
    return Response(content=twiml, media_type="application/xml")
