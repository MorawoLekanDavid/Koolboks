import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func

from chatbot.config import INVITE_TEMPLATE, OTP_TEMPLATE, PUBLIC_BASE_URL, log
from chatbot.core import redis_client
from chatbot.core.security import hash_password
from chatbot.database import get_db
from chatbot.dependencies import AGENT_SESSION_TTL, require_admin
from chatbot.models import Agent
from chatbot.routers.admin_auth import AGENT_ROLES
from chatbot.services.whatsapp_service import send_whatsapp_template
from chatbot.utils.phone import normalize_phone

router = APIRouter(tags=["invite-auth"])

INVITE_TOKEN_TTL_HOURS = 24
REGISTRATION_OTP_TTL = 300   # 5 min
RESET_OTP_TTL = 900          # 15 min
MAX_OTP_ATTEMPTS = 5
MAX_RESET_REQUESTS_PER_WINDOW = 3


def _otp_key(purpose: str, agent_id: int) -> str:
    return f"koolbuy:{purpose}:{agent_id}"


def _otp_attempts_key(purpose: str, agent_id: int) -> str:
    return f"koolbuy:{purpose}_attempts:{agent_id}"


def _gen_otp() -> str:
    return f"{secrets.randbelow(1000000):06d}"


async def _check_otp(purpose: str, agent_id: int, code: str) -> tuple[bool, str]:
    """Verifies code against the cached OTP, with a hard attempt cap so a
    6-digit code (1M possibilities) can't be brute-forced within its TTL
    window."""
    if not redis_client.client:
        return False, "Service temporarily unavailable — please try again shortly."
    attempts_key = _otp_attempts_key(purpose, agent_id)
    attempts = await redis_client.client.incr(attempts_key)
    if attempts == 1:
        await redis_client.client.expire(attempts_key, RESET_OTP_TTL)
    if attempts > MAX_OTP_ATTEMPTS:
        return False, "Too many incorrect attempts. Please request a new code."
    stored = await redis_client.client.get(_otp_key(purpose, agent_id))
    if not stored:
        return False, "This code has expired. Please request a new one."
    if stored != code.strip():
        return False, "Incorrect code."
    await redis_client.client.delete(_otp_key(purpose, agent_id))
    await redis_client.client.delete(attempts_key)
    return True, ""


async def _create_session(agent: Agent) -> dict:
    token = str(uuid.uuid4())
    session_data = json.dumps({
        "role": agent.role, "name": agent.name, "email": agent.email, "agent_id": agent.id,
    })
    if redis_client.client:
        await redis_client.client.set(f"koolbuy:agent_session:{token}", session_data, ex=AGENT_SESSION_TTL)
    return {"token": token, "name": agent.name, "role": agent.role, "email": agent.email}


async def _purge_sessions_for_agent(agent_id: int) -> None:
    """Kills every active session for this agent — used after a password
    reset in case the account was compromised and someone's already logged
    in as them."""
    if not redis_client.client:
        return
    async for key in redis_client.client.scan_iter(match="koolbuy:agent_session:*"):
        raw = await redis_client.client.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("agent_id") == agent_id:
            await redis_client.client.delete(key)


def _lookup_by_identifier(db, identifier: str):
    identifier = identifier.strip()
    q = db.query(Agent).filter(Agent.is_active == True)
    if "@" in identifier:
        return q.filter(func.lower(Agent.email) == identifier.lower()).first()
    try:
        norm = normalize_phone(identifier)
    except Exception:
        return None
    return q.filter(Agent.phone_number == norm).first()


# ── Admin: invite a new member ───────────────────────────────────────────

class InviteIn(BaseModel):
    name: str
    email: str
    phone_number: str
    role: str = "customer_success_agent"
    department_id: Optional[int] = None


async def _issue_invite(agent_id: Optional[int], name: str, email: str, phone: str, role: str, department_id) -> tuple[Agent, str]:
    def _write():
        db = get_db()
        try:
            token = secrets.token_urlsafe(32)
            expires = datetime.utcnow() + timedelta(hours=INVITE_TOKEN_TTL_HOURS)
            if agent_id:
                agent = db.query(Agent).filter(Agent.id == agent_id).first()
                if not agent:
                    raise HTTPException(404, "Agent not found")
                agent.invite_token = token
                agent.invite_token_expires_at = expires
            else:
                agent = Agent(
                    name=name, email=email, phone_number=phone, role=role,
                    department_id=department_id, is_active=False,
                    invite_token=token, invite_token_expires_at=expires,
                )
                db.add(agent)
            db.commit()
            db.refresh(agent)
            return agent, token
        finally:
            db.close()
    return await run_in_threadpool(_write)


@router.post("/admin/invite-member")
async def invite_member(body: InviteIn, ctx: dict = Depends(require_admin)):
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name or not email:
        raise HTTPException(400, "Name and email are required")
    try:
        phone = normalize_phone(body.phone_number)
    except Exception:
        raise HTTPException(400, "Invalid phone number")

    role = body.role if body.role in AGENT_ROLES else "customer_success_agent"
    if role in ("admin", "super_admin") and ctx.get("role") != "super_admin":
        role = "customer_success_agent"

    def _find_existing():
        db = get_db()
        try:
            return db.query(Agent).filter(
                (func.lower(Agent.email) == email) | (Agent.phone_number == phone)
            ).first()
        finally:
            db.close()

    existing = await run_in_threadpool(_find_existing)
    if existing and existing.is_active:
        raise HTTPException(409, "An active account with this email or phone already exists.")

    if existing:
        # Pending invite that was never completed — refresh it rather than
        # erroring, so an admin can just retry without a "Remove" round trip.
        agent, token = await _issue_invite(existing.id, name, email, phone, role, body.department_id)
    else:
        agent, token = await _issue_invite(None, name, email, phone, role, body.department_id)

    invite_link = f"{PUBLIC_BASE_URL}/admin?token={token}"
    sent = await send_whatsapp_template(phone, INVITE_TEMPLATE, [name, invite_link])
    if not sent:
        log.warning(f"Invite WhatsApp send failed for {email} — share this link manually: {invite_link}")
    return {
        "id": agent.id, "name": agent.name, "email": agent.email,
        "invite_link": invite_link, "whatsapp_sent": sent,
    }


@router.post("/admin/agents/{agent_id}/resend-invite")
async def resend_invite(agent_id: int, ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            agent = db.query(Agent).filter(Agent.id == agent_id).first()
            if not agent:
                raise HTTPException(404, "Agent not found")
            if agent.is_active:
                raise HTTPException(400, "This account is already active.")
            return agent
        finally:
            db.close()

    existing = await run_in_threadpool(_fetch)
    agent, token = await _issue_invite(existing.id, existing.name, existing.email, existing.phone_number, existing.role, existing.department_id)
    invite_link = f"{PUBLIC_BASE_URL}/admin?token={token}"
    sent = await send_whatsapp_template(agent.phone_number, INVITE_TEMPLATE, [agent.name, invite_link])
    return {"invite_link": invite_link, "whatsapp_sent": sent}


# ── Public: accept an invite ─────────────────────────────────────────────

@router.get("/auth/verify-invite")
async def verify_invite(token: str):
    def _check():
        db = get_db()
        try:
            agent = db.query(Agent).filter(Agent.invite_token == token).first()
            if not agent or agent.is_active:
                return None
            if not agent.invite_token_expires_at or agent.invite_token_expires_at < datetime.utcnow():
                return None
            return {"valid": True, "name": agent.name, "email": agent.email, "role": agent.role}
        finally:
            db.close()
    result = await run_in_threadpool(_check)
    if not result:
        raise HTTPException(400, "This invite link is invalid or has expired. Ask an admin to resend it.")
    return result


class CompleteRegistrationIn(BaseModel):
    token: str
    password: str
    confirm_password: str


@router.post("/auth/complete-registration")
async def complete_registration(body: CompleteRegistrationIn):
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if body.password != body.confirm_password:
        raise HTTPException(400, "Passwords do not match.")

    def _activate():
        db = get_db()
        try:
            agent = db.query(Agent).filter(Agent.invite_token == body.token).first()
            if not agent or agent.is_active:
                raise HTTPException(400, "This invite link is invalid or has already been used.")
            if not agent.invite_token_expires_at or agent.invite_token_expires_at < datetime.utcnow():
                raise HTTPException(400, "This invite link has expired. Ask an admin to resend it.")
            agent.password_hash = hash_password(body.password)
            agent.is_active = True
            agent.invite_token = None
            agent.invite_token_expires_at = None
            db.commit()
            db.refresh(agent)
            return agent
        finally:
            db.close()

    agent = await run_in_threadpool(_activate)

    otp_sent = False
    if agent.phone_number and redis_client.client:
        otp = _gen_otp()
        await redis_client.client.set(_otp_key("auth_otp", agent.id), otp, ex=REGISTRATION_OTP_TTL)
        otp_sent = await send_whatsapp_template(agent.phone_number, OTP_TEMPLATE, [otp])

    # Password + is_active are already committed regardless of OTP delivery —
    # the normal email/password login always works from here even if
    # WhatsApp is down, so nobody gets stuck mid-registration.
    return {
        "ok": True, "email": agent.email, "otp_sent": otp_sent,
        "message": "Account created." + (
            " A verification code was sent to your WhatsApp."
            if otp_sent else
            " We couldn't send a verification code right now — just log in with your email and password instead."
        ),
    }


class VerifyOtpIn(BaseModel):
    email: str
    otp_code: str


@router.post("/auth/verify-otp")
async def verify_registration_otp(body: VerifyOtpIn):
    email = body.email.strip().lower()

    def _lookup():
        db = get_db()
        try:
            return db.query(Agent).filter(func.lower(Agent.email) == email, Agent.is_active == True).first()
        finally:
            db.close()

    agent = await run_in_threadpool(_lookup)
    if not agent:
        raise HTTPException(404, "Account not found.")

    ok, err = await _check_otp("auth_otp", agent.id, body.otp_code)
    if not ok:
        raise HTTPException(403, err)

    return await _create_session(agent)


# ── Public: forgot password ──────────────────────────────────────────────

class ForgotPasswordIn(BaseModel):
    email_or_phone: str


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordIn):
    def _lookup():
        db = get_db()
        try:
            return _lookup_by_identifier(db, body.email_or_phone)
        finally:
            db.close()

    agent = await run_in_threadpool(_lookup)

    # Always the same generic response whether or not the account exists —
    # this endpoint must not leak who's registered.
    generic = {"ok": True, "message": "If that account exists, a verification code has been sent to its WhatsApp number."}

    if not agent or not agent.phone_number or not redis_client.client:
        return generic

    rl_key = f"koolbuy:password_reset_rl:{agent.id}"
    count = await redis_client.client.incr(rl_key)
    if count == 1:
        await redis_client.client.expire(rl_key, RESET_OTP_TTL)
    if count > MAX_RESET_REQUESTS_PER_WINDOW:
        return generic  # drop silently — still looks identical to the caller

    otp = _gen_otp()
    await redis_client.client.set(_otp_key("password_reset", agent.id), otp, ex=RESET_OTP_TTL)
    await send_whatsapp_template(agent.phone_number, OTP_TEMPLATE, [otp])
    return generic


class ResetPasswordIn(BaseModel):
    email_or_phone: str
    otp_code: str
    new_password: str


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordIn):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters.")

    def _lookup():
        db = get_db()
        try:
            return _lookup_by_identifier(db, body.email_or_phone)
        finally:
            db.close()

    agent = await run_in_threadpool(_lookup)
    if not agent:
        raise HTTPException(400, "Invalid code or account.")

    ok, err = await _check_otp("password_reset", agent.id, body.otp_code)
    if not ok:
        raise HTTPException(403, err)

    def _update():
        db = get_db()
        try:
            a = db.query(Agent).filter(Agent.id == agent.id).first()
            a.password_hash = hash_password(body.new_password)
            db.commit()
        finally:
            db.close()

    await run_in_threadpool(_update)
    await _purge_sessions_for_agent(agent.id)
    return {"ok": True}
