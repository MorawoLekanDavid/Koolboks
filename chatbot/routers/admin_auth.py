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
from chatbot.models import Agent, Department
from chatbot.routers.permissions import require_tab_permission
from chatbot.services.presence_service import get_status, get_statuses

router = APIRouter(prefix="/admin", tags=["admin-auth"])


@router.get("/me")
async def get_me(ctx: dict = Depends(get_admin_ctx)):
    agent_id = ctx.get("agent_id")
    department_id = None
    status = "offline"
    if agent_id:
        def _fetch():
            db = get_db()
            try:
                return db.query(Agent).filter(Agent.id == agent_id).first()
            finally:
                db.close()
        agent = await run_in_threadpool(_fetch)
        if agent:
            department_id = agent.department_id
        status = await get_status(agent_id)
    return {
        "role": ctx.get("role", "customer_success_agent"),
        "name": ctx.get("name", ""),
        "email": ctx.get("email", ""),
        "agent_id": agent_id,
        "department_id": department_id,
        "status": status,
    }


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

AGENT_ROLES = ("customer_success_agent", "telesales_agent", "admin", "bi_analyst", "team_lead")


class AgentCreate(BaseModel):
    name: str
    email: str
    password: str
    role: Optional[str] = "customer_success_agent"


@router.get("/agents")
async def list_agents(ctx: dict = Depends(require_tab_permission("team"))):
    def _fetch():
        db = get_db()
        try:
            agents = db.query(Agent).order_by(Agent.created_at.asc()).all()
            depts = {d.id: d.name for d in db.query(Department).all()}
            return agents, depts
        finally:
            db.close()
    agents, depts = await run_in_threadpool(_fetch)
    statuses = await get_statuses([a.id for a in agents])
    return [{"id": a.id, "name": a.name, "email": a.email, "role": a.role,
             "phone_number": a.phone_number, "is_active": a.is_active,
             "department_id": a.department_id, "department_name": depts.get(a.department_id),
             "status": statuses.get(a.id, "offline"),
             "created_at": a.created_at.isoformat() if a.created_at else None} for a in agents]


@router.get("/agents/directory")
async def agents_directory(ctx: dict = Depends(get_admin_ctx)):
    """Minimal agent list (name/email/role only) for every logged-in user,
    regardless of "team" permission — used to populate "assign this
    conversation/lead to..." dropdowns, which are a normal action available
    to any agent, not a Team-tab management capability."""
    def _fetch():
        db = get_db()
        try:
            # Pending invites can't be assigned anything yet — only surface
            # accounts that can actually log in and act on a conversation.
            agents = db.query(Agent).filter(Agent.is_active == True).order_by(Agent.name.asc()).all()
            return [{"id": a.id, "name": a.name, "email": a.email, "role": a.role,
                     "department_id": a.department_id} for a in agents]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


# ── Departments ─────────────────────────────────────────────────────────────

class DepartmentIn(BaseModel):
    name: str
    team_lead_id: Optional[int] = None


@router.get("/departments")
async def list_departments(ctx: dict = Depends(require_tab_permission("team"))):
    def _fetch():
        db = get_db()
        try:
            depts = db.query(Department).order_by(Department.name.asc()).all()
            agent_names = {a.id: a.name for a in db.query(Agent).all()}
            member_counts = {}
            for a in db.query(Agent).filter(Agent.department_id.isnot(None)).all():
                member_counts[a.department_id] = member_counts.get(a.department_id, 0) + 1
            return [
                {"id": d.id, "name": d.name, "team_lead_id": d.team_lead_id,
                 "team_lead_name": agent_names.get(d.team_lead_id),
                 "member_count": member_counts.get(d.id, 0)}
                for d in depts
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.post("/departments")
async def create_department(body: DepartmentIn, ctx: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Department name is required")
    db = get_db()
    try:
        existing = db.query(Department).filter(Department.name == name).first()
        if existing:
            raise HTTPException(409, "A department with this name already exists")
        dept = Department(name=name, team_lead_id=body.team_lead_id)
        db.add(dept)
        db.commit()
        db.refresh(dept)
        return {"id": dept.id, "name": dept.name, "team_lead_id": dept.team_lead_id}
    finally:
        db.close()


@router.patch("/departments/{dept_id}")
async def update_department(dept_id: int, body: DepartmentIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        dept = db.query(Department).filter(Department.id == dept_id).first()
        if not dept:
            raise HTTPException(404, "Department not found")
        name = body.name.strip()
        if name and name != dept.name:
            clash = db.query(Department).filter(Department.name == name).first()
            if clash:
                raise HTTPException(409, "A department with this name already exists")
            dept.name = name
        dept.team_lead_id = body.team_lead_id
        db.commit()
        return {"id": dept.id, "name": dept.name, "team_lead_id": dept.team_lead_id}
    finally:
        db.close()


@router.delete("/departments/{dept_id}")
async def delete_department(dept_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        dept = db.query(Department).filter(Department.id == dept_id).first()
        if not dept:
            raise HTTPException(404, "Department not found")
        db.query(Agent).filter(Agent.department_id == dept_id).update({"department_id": None})
        db.delete(dept)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


class AgentDepartmentIn(BaseModel):
    department_id: Optional[int] = None


@router.patch("/agents/{agent_id}/department")
async def set_agent_department(agent_id: int, body: AgentDepartmentIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise HTTPException(404, "Agent not found")
        if body.department_id is not None:
            dept = db.query(Department).filter(Department.id == body.department_id).first()
            if not dept:
                raise HTTPException(404, "Department not found")
        agent.department_id = body.department_id
        db.commit()
        return {"id": agent.id, "department_id": agent.department_id}
    finally:
        db.close()


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
    role: str


@router.patch("/agents/{agent_id}/role")
async def update_agent_role(agent_id: int, body: RoleUpdate, ctx: dict = Depends(require_super_admin)):
    if body.role not in AGENT_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(AGENT_ROLES)}")
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
