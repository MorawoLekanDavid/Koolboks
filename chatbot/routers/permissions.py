from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import RolePermission

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
        "analytics": False, "team": False, "templates": False, "aiSettings": False, "usage": False, "routing": False,
    },
    "telesales_agent": {
        "leads": False, "contacts": False, "products": False, "canned": False,
        "analytics": False, "team": False, "templates": True, "aiSettings": False, "usage": False, "routing": False,
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


async def require_conversation_write(ctx: dict = Depends(get_admin_ctx)) -> dict:
    """Blocks READ_ONLY_CONVERSATION_ROLES (bi_analyst) from any endpoint that
    mutates a conversation — reply, handoff, tag, template send, reassign.
    Conversations itself has no tab permission gate (always visible to
    everyone), so this is enforced by role rather than by the tab matrix."""
    if ctx.get("role") in READ_ONLY_CONVERSATION_ROLES:
        raise HTTPException(403, "Your role has read-only access to conversations")
    return ctx


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
