from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbot.routers.permissions import require_tab_permission
from chatbot.services.routing_service import is_routing_enabled, set_routing_enabled

router = APIRouter(prefix="/admin/routing", tags=["routing"])


@router.get("/config")
async def get_routing_config(ctx: dict = Depends(require_tab_permission("routing"))):
    return {"enabled": await is_routing_enabled()}


class RoutingConfigIn(BaseModel):
    enabled: bool


@router.put("/config")
async def update_routing_config(body: RoutingConfigIn, ctx: dict = Depends(require_tab_permission("routing"))):
    await set_routing_enabled(body.enabled)
    return {"enabled": body.enabled}
