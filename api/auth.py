"""Auth: email+password -> JWT, role-based route protection.

Stdlib only (hmac JWT HS256, scrypt password hashing) — no new dependencies.

Roles:
  platform_admin  -> /admin/* and every /owner/{slug}/*
  tenant_admin    -> /owner/{slug}/* for its own business only

Route classes (enforced by middleware):
  /auth/*                       open
  /agent/*                      internal (loopback media plane). If AGENT_TOKEN
                                is set, requires X-Agent-Token to match.
  /admin/*                      platform_admin
  /owner/{slug}/*               platform_admin or that tenant's tenant_admin
  /login, /app, /admin-ui, ...  open (the SPAs do auth client-side)

Env:
  AUTH_SECRET     JWT signing secret (REQUIRED in prod; dev default is insecure)
  AUTH_DISABLED=1 bypass all checks (local development only)
  AGENT_TOKEN     shared secret for /agent/* when the control plane is exposed
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .models import Business, User, UserRole

AUTH_SECRET = os.getenv("AUTH_SECRET", "dev-secret-change-me")
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0") == "1"
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", str(12 * 3600)))

# ------------------------------ passwords ------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1)
    return f"scrypt${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, hexhash = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1)
        return hmac.compare_digest(dk.hex(), hexhash)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------ JWT (HS256) ------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user: User, slug: str | None) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": user.id, "email": user.email, "role": user.role,
        "slug": slug, "exp": int(time.time()) + TOKEN_TTL,
    }).encode())
    sig = hmac.new(AUTH_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        header, payload, sig = token.split(".")
        expected = hmac.new(AUTH_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        claims = json.loads(_unb64(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:  # noqa: BLE001
        return None


# ------------------------------ middleware ------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if AUTH_DISABLED:
            return await call_next(request)
        path = request.url.path

        if path.startswith("/agent/"):
            if AGENT_TOKEN and request.headers.get("x-agent-token") != AGENT_TOKEN:
                return JSONResponse({"detail": "agent token required"}, status_code=401)
            return await call_next(request)

        needs_admin = path.startswith("/admin/")
        needs_owner = path.startswith("/owner/")
        if not (needs_admin or needs_owner):
            return await call_next(request)   # /auth/*, SPAs, /docs, /twilio...

        authz = request.headers.get("authorization", "")
        claims = decode_token(authz.removeprefix("Bearer ").strip()) if authz.startswith("Bearer ") else None
        if not claims:
            return JSONResponse({"detail": "login required"}, status_code=401)
        role = claims.get("role")
        if needs_admin and role != UserRole.platform_admin:
            return JSONResponse({"detail": "platform admin only"}, status_code=403)
        if needs_owner and role != UserRole.platform_admin:
            slug = path.split("/")[2] if len(path.split("/")) > 2 else ""
            if role != UserRole.tenant_admin or claims.get("slug") != slug:
                return JSONResponse({"detail": "no access to this tenant"}, status_code=403)
        request.state.user = claims
        return await call_next(request)


# ------------------------------ routes ------------------------------

def wire(app, db):
    app.add_middleware(AuthMiddleware)

    class LoginBody(BaseModel):
        email: str
        password: str

    @app.post("/auth/login")
    def login(body: LoginBody):
        from .main import engine  # local import to avoid cycle
        with Session(engine) as s:
            user = s.exec(select(User).where(User.email == body.email.lower().strip())).first()
            if not user or not verify_password(body.password, user.password_hash):
                raise HTTPException(401, "invalid email or password")
            slug = None
            if user.business_id:
                biz = s.get(Business, user.business_id)
                slug = biz.slug if biz else None
            return {"token": make_token(user, slug), "role": user.role, "slug": slug,
                    "email": user.email}

    @app.get("/auth/me")
    def me(request: Request):
        authz = request.headers.get("authorization", "")
        claims = decode_token(authz.removeprefix("Bearer ").strip()) if authz.startswith("Bearer ") else None
        if not claims:
            raise HTTPException(401, "login required")
        return claims

    class CreateUserBody(BaseModel):
        email: str
        password: str
        role: UserRole = UserRole.tenant_admin
        slug: str | None = None

    @app.post("/admin/users")
    def create_user(body: CreateUserBody, request: Request):
        from .main import engine
        with Session(engine) as s:
            if s.exec(select(User).where(User.email == body.email.lower().strip())).first():
                raise HTTPException(409, "email exists")
            business_id = None
            if body.role == UserRole.tenant_admin:
                if not body.slug:
                    raise HTTPException(422, "tenant_admin needs a slug")
                biz = s.exec(select(Business).where(Business.slug == body.slug)).first()
                if not biz:
                    raise HTTPException(404, f"unknown tenant '{body.slug}'")
                business_id = biz.id
            user = User(email=body.email.lower().strip(),
                        password_hash=hash_password(body.password),
                        role=body.role, business_id=business_id)
            s.add(user)
            s.commit()
            return {"ok": True, "user_id": user.id}

    @app.get("/admin/users")
    def list_users(request: Request):
        from .main import engine
        with Session(engine) as s:
            users = s.exec(select(User)).all()
            biz = {b.id: b.slug for b in s.exec(select(Business)).all()}
            return [{"id": u.id, "email": u.email, "role": u.role,
                     "slug": biz.get(u.business_id)} for u in users]
