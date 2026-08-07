from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import Agent, ConversationOwner, Lead, RolePermission
from chatbot.utils.phone import normalize_phone

router = APIRouter(prefix="/admin/role-permissions", tags=["permissions"])

# Tabs that can be toggled per role (conv is always visible — not in this list)
ALL_TABS = ["leads", "contacts", "products", "canned", "analytics", "team", "templates", "aiSettings", "usage", "routing"]

# Roles whose permissions are configurable via the Team tab UI
CONFIGURABLE_ROLES = [
    "super_admin", "admin",
    "customer_success_agent", "telesales_agent", "bi_analyst", "team_lead",
]

# Roles that only ever get read access to Conversations — no send/take-over/
# tag/reassign, regardless of what the tab matrix says. Not part of the
# per-tab matrix since it's a property of the role itself, not a toggle.
READ_ONLY_CONVERSATION_ROLES = ("bi_analyst",)

# Default permissions used as fallback when no DB row exists
DEFAULTS: Dict[str, Dict[str, bool]] = {
    "super_admin": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": True, "team": True, "templates": True, "aiSettings": True, "usage": True, "routing": True,
    },
    "admin": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": True, "team": True, "templates": True, "aiSettings": True, "usage": True, "routing": True,
    },
    "customer_success_agent": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": True, "team": False, "templates": False, "aiSettings": False, "usage": False, "routing": False,
    },
    "telesales_agent": {
        "leads": False, "contacts": False, "products": False, "canned": False,
        "analytics": True, "team": False, "templates": True, "aiSettings": False, "usage": False, "routing": False,
    },
    "bi_analyst": {
        "leads": False, "contacts": False, "products": False, "canned": False,
        "analytics": True, "team": False, "templates": False, "aiSettings": False, "usage": True, "routing": False,
    },
    "team_lead": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": True, "team": False, "templates": False, "aiSettings": False, "usage": False, "routing": False,
    },
}


def _build_role_perms(db, role: str) -> Dict[str, bool]:
    """Return tab permissions for a role, merging DB overrides on top of defaults."""
    perms = dict(DEFAULTS.get(role, {tab: True for tab in ALL_TABS}))
    rows = db.query(RolePermission).filter(RolePermission.role == role).all()
    for row in rows:
        perms[row.tab] = row.allowed
    perms["conv"] = True  # conversations tab is always on
    return perms


def require_tab_permission(tab: str):
    """Dependency factory for enforcing the configurable Role Permissions matrix
    on actual API endpoints, not just frontend nav visibility. admin/super_admin
    always pass; every other role must have `tab` enabled in the matrix."""
    async def _check(ctx: dict = Depends(get_admin_ctx)) -> dict:
        role = ctx.get("role")
        if role in ("admin", "super_admin"):
            return ctx

        def _fetch():
            db = get_db()
            try:
                return _build_role_perms(db, role)
            finally:
                db.close()
        perms = await run_in_threadpool(_fetch)
        if not perms.get(tab, False):
            raise HTTPException(403, f"Your role does not have access to {tab}")
        return ctx
    return _check


# Roles that see every conversation regardless of assignment — bi_analyst is
# read-only (see READ_ONLY_CONVERSATION_ROLES) but still needs full visibility
# to do reporting/audit work.
CONVERSATION_UNRESTRICTED_ROLES = ("admin", "super_admin", "bi_analyst")


def _dept_emails(db, agent_id: Optional[int]) -> set:
    if not agent_id:
        return set()
    me = db.query(Agent).filter(Agent.id == agent_id).first()
    if not me or me.department_id is None:
        return set()
    return {a.email for a in db.query(Agent).filter(Agent.department_id == me.department_id).all()}


def get_conversation_scope(db, ctx: dict) -> dict:
    """Computes what a role is allowed to see in Conversations, once per
    request — used both to filter the list and to gate a single phone.
    - admin/super_admin/bi_analyst: unrestricted.
    - team_lead: their own department's assigned chats, plus anything
      unassigned (so a Team Lead can hand out new chats, per the "only Team
      Lead and Admin can see/hand out unassigned chats" rule).
    - everyone else: only conversations assigned to them personally —
      unassigned chats are invisible until routing or a Team Lead assigns one.
    """
    role = ctx.get("role")
    if role in CONVERSATION_UNRESTRICTED_ROLES:
        return {"unrestricted": True}
    if role == "team_lead":
        return {"unrestricted": False, "team_lead": True, "dept_emails": _dept_emails(db, ctx.get("agent_id")), "include_unassigned": True}
    return {"unrestricted": False, "team_lead": False, "own_email": ctx.get("email"), "include_unassigned": False}


# Roles that see every agent's numbers in the Analytics Console, unscoped.
ANALYTICS_UNRESTRICTED_ROLES = ("admin", "super_admin", "bi_analyst")


def get_analytics_scope(db, ctx: dict) -> dict:
    """Computes what a role is allowed to see in the Analytics Console's
    agent-scoped tabs (Agent Performance, Shift & Login — the only two with
    a per-agent breakdown in the data).
    - admin/super_admin/bi_analyst: unrestricted (org-wide).
    - team_lead: their own department's agents only.
    - everyone else: themselves only.
    Tabs with no agent dimension at all (Overview, AI Performance, Traffic,
    Campaigns) are gated separately by require_org_wide_analytics below —
    this scope has nothing to filter them by."""
    role = ctx.get("role")
    if role in ANALYTICS_UNRESTRICTED_ROLES:
        return {"unrestricted": True}
    if role == "team_lead":
        agent_id = ctx.get("agent_id")
        me = db.query(Agent).filter(Agent.id == agent_id).first() if agent_id else None
        if me and me.department_id is not None:
            dept_agents = db.query(Agent).filter(Agent.department_id == me.department_id).all()
            return {"unrestricted": False, "agent_ids": {a.id for a in dept_agents}, "agent_names": {a.name for a in dept_agents}}
        return {"unrestricted": False, "agent_ids": set(), "agent_names": set()}
    agent_id = ctx.get("agent_id")
    name = ctx.get("name")
    return {"unrestricted": False, "agent_ids": {agent_id} if agent_id else set(), "agent_names": {name} if name else set()}


async def require_org_wide_analytics(ctx: dict = Depends(require_tab_permission("analytics"))) -> dict:
    """Gates the Analytics Console tabs that have no per-agent breakdown
    (Overview, AI Performance, Traffic Telemetry, Campaigns) — there's no
    way to scope company-wide numbers down to "my own", so regular agents
    don't get an unscoped view of them at all."""
    if ctx.get("role") not in ANALYTICS_UNRESTRICTED_ROLES and ctx.get("role") != "team_lead":
        raise HTTPException(403, "Your role only has access to your own analytics")
    return ctx


def _phone_allowed(db, scope: dict, phone: str, claim: bool = False) -> bool:
    if scope["unrestricted"]:
        return True
    norm = normalize_phone(phone)
    owner = db.query(ConversationOwner).filter(ConversationOwner.phone == norm).first()
    if not owner:
        # No owner yet: normally only Team Lead/Admin can see/claim an
        # unassigned chat (so it doesn't get lost among an agent's own
        # inbox). But `claim=True` is for actions that ARE the claim —
        # proactively messaging a lead for the first time — where forcing
        # every agent's outreach through a Team Lead first would defeat
        # the point of letting them work their own leads.
        return scope["include_unassigned"] or claim
    if scope["team_lead"]:
        return owner.owner_email in scope["dept_emails"]
    return owner.owner_email == scope["own_email"]


def conversation_guard(write: bool = False, claim: bool = False):
    """Dependency factory for any endpoint keyed by a {phone} path param.
    write=True additionally blocks READ_ONLY_CONVERSATION_ROLES (bi_analyst)
    — they can view any conversation but never send/take-over/tag/reassign.
    claim=True allows touching a phone with no owner yet regardless of role
    — see _phone_allowed."""
    async def _check(phone: str, ctx: dict = Depends(get_admin_ctx)) -> dict:
        if write and ctx.get("role") in READ_ONLY_CONVERSATION_ROLES:
            raise HTTPException(403, "Your role has read-only access to conversations")

        def _fetch():
            db = get_db()
            try:
                scope = get_conversation_scope(db, ctx)
                return _phone_allowed(db, scope, phone, claim=claim)
            finally:
                db.close()
        if not await run_in_threadpool(_fetch):
            raise HTTPException(403, "You don't have access to this conversation")
        return ctx
    return _check


def get_lead_scope(db, ctx: dict) -> dict:
    """Computes what a role is allowed to see in the Leads tab (Interested +
    Drop-off), same philosophy as get_conversation_scope.
    - admin/super_admin/bi_analyst: unrestricted.
    - team_lead: leads assigned to their own department's agents, plus
      unassigned ones (so they can hand them out).
    - everyone else: only leads assigned to them personally — unassigned
      leads are invisible, same as an unassigned conversation."""
    role = ctx.get("role")
    if role in CONVERSATION_UNRESTRICTED_ROLES:
        return {"unrestricted": True}
    if role == "team_lead":
        agent_id = ctx.get("agent_id")
        me = db.query(Agent).filter(Agent.id == agent_id).first() if agent_id else None
        dept_names = set()
        if me and me.department_id is not None:
            dept_names = {a.name for a in db.query(Agent).filter(Agent.department_id == me.department_id).all()}
        return {"unrestricted": False, "team_lead": True, "dept_names": dept_names, "include_unassigned": True}
    return {"unrestricted": False, "team_lead": False, "own_name": ctx.get("name"), "include_unassigned": False}


def _lead_allowed(db, scope: dict, phone: str) -> bool:
    if scope["unrestricted"]:
        return True
    norm = normalize_phone(phone)
    lead = db.query(Lead).filter(Lead.phone == norm).first()
    if not lead:
        # No Lead row at all (e.g. a Drop-off — chatted but never captured a
        # phone/became a qualified lead) — nothing assigned, nothing to see.
        return False
    if scope["team_lead"]:
        return lead.assigned_to in scope["dept_names"] or (scope["include_unassigned"] and not lead.assigned_to)
    return lead.assigned_to == scope["own_name"]


def lead_guard():
    """Dependency factory for any Leads endpoint keyed by a {phone} path
    param — read/write access follows get_lead_scope exactly (only the
    assigned agent, their team lead, or admin can view/act on a lead)."""
    async def _check(phone: str, ctx: dict = Depends(get_admin_ctx)) -> dict:
        def _fetch():
            db = get_db()
            try:
                scope = get_lead_scope(db, ctx)
                return _lead_allowed(db, scope, phone)
            finally:
                db.close()
        if not await run_in_threadpool(_fetch):
            raise HTTPException(403, "You don't have access to this lead")
        return ctx
    return _check


def require_any_tab_permission(tabs: list[str]):
    """Like require_tab_permission, but passes if the role has ANY of the given
    tabs enabled. Used where one endpoint legitimately serves more than one tab
    — e.g. the contact list is read both by the Contacts tab and by Broadcast's
    audience filter, so a role with only "templates" (no "contacts") still needs
    read access to build a broadcast audience."""
    async def _check(ctx: dict = Depends(get_admin_ctx)) -> dict:
        role = ctx.get("role")
        if role in ("admin", "super_admin"):
            return ctx

        def _fetch():
            db = get_db()
            try:
                return _build_role_perms(db, role)
            finally:
                db.close()
        perms = await run_in_threadpool(_fetch)
        if not any(perms.get(tab, False) for tab in tabs):
            raise HTTPException(403, f"Your role does not have access to {' or '.join(tabs)}")
        return ctx
    return _check


@router.get("")
async def get_all_permissions(ctx: dict = Depends(require_admin)):
    """Full matrix for all configurable roles — used to render the Team tab matrix."""
    def _fetch():
        db = get_db()
        try:
            return {role: _build_role_perms(db, role) for role in CONFIGURABLE_ROLES}
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.get("/for/{role}")
async def get_permissions_for_role(role: str, ctx: dict = Depends(get_admin_ctx)):
    """Permissions for a single role — called at login time to wire up the nav."""
    def _fetch():
        db = get_db()
        try:
            return _build_role_perms(db, role)
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


class PermissionUpdateBody(BaseModel):
    permissions: Dict[str, Dict[str, bool]]  # {role: {tab: allowed}}


@router.put("")
async def update_permissions(body: PermissionUpdateBody, ctx: dict = Depends(require_admin)):
    """Save the full permissions matrix from the Team tab UI."""
    def _save():
        db = get_db()
        try:
            for role, tabs in body.permissions.items():
                if role not in CONFIGURABLE_ROLES:
                    continue
                for tab, allowed in tabs.items():
                    if tab == "conv":
                        continue  # conv is always on, never stored
                    if tab not in ALL_TABS:
                        continue
                    row = db.query(RolePermission).filter(
                        RolePermission.role == role,
                        RolePermission.tab == tab,
                    ).first()
                    if row:
                        row.allowed = allowed
                    else:
                        db.add(RolePermission(role=role, tab=tab, allowed=allowed))
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    return await run_in_threadpool(_save)
