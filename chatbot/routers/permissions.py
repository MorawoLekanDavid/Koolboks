from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import RolePermission

router = APIRouter(prefix="/admin/role-permissions", tags=["permissions"])

# Tabs that can be toggled per role (conv is always visible — not in this list)
ALL_TABS = ["leads", "contacts", "products", "canned", "analytics", "team", "templates", "aiSettings"]

# Roles whose permissions are configurable via the Team tab UI
CONFIGURABLE_ROLES = [
    "super_admin", "admin",
    "agent", "customer_success_agent", "telesales_agent", "sales_agent",
]

# Default permissions used as fallback when no DB row exists
DEFAULTS: Dict[str, Dict[str, bool]] = {
    "super_admin": {
        "leads": True, "contacts": True, "products": True, "canned": False,
        "analytics": True, "team": True, "templates": True, "aiSettings": True,
    },
    "admin": {
        "leads": True, "contacts": True, "products": True, "canned": False,
        "analytics": True, "team": True, "templates": True, "aiSettings": True,
    },
    "agent": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": False, "team": False, "templates": False, "aiSettings": False,
    },
    "customer_success_agent": {
        "leads": True, "contacts": True, "products": True, "canned": True,
        "analytics": False, "team": False, "templates": False, "aiSettings": False,
    },
    "telesales_agent": {
        "leads": False, "contacts": False, "products": False, "canned": False,
        "analytics": False, "team": False, "templates": True, "aiSettings": False,
    },
    "sales_agent": {
        "leads": False, "contacts": False, "products": False, "canned": False,
        "analytics": False, "team": False, "templates": True, "aiSettings": False,
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
