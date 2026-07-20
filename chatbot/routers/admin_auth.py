import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import func

from chatbot.config import ADMIN_KEY, log
from chatbot.core import redis_client
from chatbot.core.security import hash_password, verify_password
from chatbot.database import get_db
from chatbot.dependencies import AGENT_SESSION_TTL, get_admin_ctx, require_admin, require_super_admin
from chatbot.models import Agent

router = APIRouter(prefix="/admin", tags=["admin-auth"])


@router.get("/me")
async def get_me(ctx: dict = Depends(get_admin_ctx)):
    return {"role": ctx.get("role", "agent"), "name": ctx.get("name", ""), "email": ctx.get("email", "")}


class AgentLoginRequest(BaseModel):
    email: str
    password: str
    admin_key: Optional[str] = None  # provided only when registering/logging in as super_admin


@router.post("/agent-login")
async def agent_login(body: AgentLoginRequest):
    db = get_db()
    try:
        email = body.email.strip().lower()

        # Super admin path: valid key provided → register or login as super_admin
        if body.admin_key and body.admin_key == ADMIN_KEY:
            agent = db.query(Agent).filter(func.lower(Agent.email) == email).first()
            if not agent:
                # First-time super_admin registration
                if not body.password:
                    raise HTTPException(400, "Password is required to create your account.")
                agent = Agent(name=body.email.split("@")[0].capitalize(),
                              email=email,
                              password_hash=hash_password(body.password),
                              role="super_admin")
                db.add(agent)
                db.commit()
                db.refresh(agent)
                log.info(f"Super admin account created: {email}")
            else:
                if agent.role != "super_admin":
                    raise HTTPException(403, "This email is registered as a regular agent, not super admin.")
                if not body.password:
                    raise HTTPException(400, "Password is required.")
                if not verify_password(body.password, agent.password_hash):
                    # Knowing the admin key authorizes resetting a forgotten super admin password
                    agent.password_hash = hash_password(body.password)
                    db.commit()
                    log.info(f"Super admin password reset via admin key: {email}")
            # Super admin token is always the ADMIN_KEY for backward compat
            return {"token": ADMIN_KEY, "name": agent.name, "role": "super_admin", "email": agent.email}

        # Normal login path (agent or admin)
        agent = db.query(Agent).filter(func.lower(Agent.email) == email).first()
        if not agent:
            raise HTTPException(403, "Email not registered. Contact your admin.")
        if not agent.password_hash:
            raise HTTPException(403, "Password not set. Ask your admin to reset it.")
        if not verify_password(body.password, agent.password_hash):
            raise HTTPException(403, "Incorrect password.")
        token = str(uuid.uuid4())
        session_data = json.dumps({
            "role": agent.role,
            "name": agent.name,
            "email": agent.email,
            "agent_id": agent.id,
        })
        if redis_client.client:
            await redis_client.client.set(f"koolbuy:agent_session:{token}", session_data, ex=AGENT_SESSION_TTL)
        return {"token": token, "name": agent.name, "role": agent.role, "email": agent.email}
    finally:
        db.close()


# ── Agent Management ──────────────────────────────────────────────────────────

AGENT_ROLES = ("agent", "customer_success_agent", "telesales_agent", "admin", "sales_agent")


class AgentCreate(BaseModel):
    name: str
    email: str
    password: str
    role: Optional[str] = "customer_success_agent"


@router.get("/agents")
async def list_agents(ctx: dict = Depends(require_admin)):
    def _fetch():
        db = get_db()
        try:
            agents = db.query(Agent).order_by(Agent.created_at.asc()).all()
            return [{"id": a.id, "name": a.name, "email": a.email, "role": a.role,
                     "created_at": a.created_at.isoformat() if a.created_at else None} for a in agents]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.post("/agents")
async def register_agent(body: AgentCreate, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        existing = db.query(Agent).filter(
            func.lower(Agent.email) == body.email.strip().lower()
        ).first()
        if existing:
            raise HTTPException(409, "An agent with this email already exists.")
        new_role = body.role if body.role in AGENT_ROLES else "customer_success_agent"
        if new_role in ("admin", "super_admin") and ctx.get("role") != "super_admin":
            new_role = "customer_success_agent"
        agent = Agent(
            name=body.name.strip(),
            email=body.email.strip().lower(),
            password_hash=hash_password(body.password),
            role=new_role,
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        return {"id": agent.id, "name": agent.name, "email": agent.email, "role": agent.role}
    finally:
        db.close()


class RoleUpdate(BaseModel):
    role: str  # "admin" or "agent"


@router.patch("/agents/{agent_id}/role")
async def update_agent_role(agent_id: int, body: RoleUpdate, ctx: dict = Depends(require_super_admin)):
    if body.role not in ("admin", "agent", "customer_success_agent", "telesales_agent", "sales_agent"):
        raise HTTPException(400, "Role must be one of: admin, customer_success_agent, telesales_agent, agent")
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Cannot change the super admin's role.")
        agent.role = body.role
        db.commit()
        return {"id": agent.id, "name": agent.name, "role": agent.role}
    finally:
        db.close()


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Cannot remove the super admin account.")
        if agent.role == "admin" and ctx.get("role") != "super_admin":
            raise HTTPException(403, "Only super admin can remove an admin account.")
        db.delete(agent)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


class ChangePasswordRequest(BaseModel):
    email: str
    old_password: str
    new_password: str


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest):
    """Self-service password change from the login screen — proof of identity
    is the current password, so no active session is required."""
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters.")
    db = get_db()
    try:
        email = body.email.strip().lower()
        agent = db.query(Agent).filter(func.lower(Agent.email) == email).first()
        if not agent or not agent.password_hash:
            raise HTTPException(404, "Account not found.")
        if not verify_password(body.old_password, agent.password_hash):
            raise HTTPException(403, "Current password is incorrect.")
        agent.password_hash = hash_password(body.new_password)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/agents/{agent_id}/reset-password")
async def reset_agent_password(agent_id: int, body: ResetPasswordRequest, ctx: dict = Depends(require_admin)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "New password must be at least 6 characters.")
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found.")
        if agent.role == "super_admin":
            raise HTTPException(403, "Use the login screen's admin key to reset the super admin password.")
        if agent.role == "admin" and ctx.get("role") != "super_admin":
            raise HTTPException(403, "Only super admin can reset an admin's password.")
        agent.password_hash = hash_password(body.new_password)
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
