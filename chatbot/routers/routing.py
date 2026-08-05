from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chatbot.routers.permissions import require_tab_permission
from chatbot.services import routing_service

router = APIRouter(prefix="/admin/routing", tags=["routing"])


@router.get("/config")
async def get_config(ctx: dict = Depends(require_tab_permission("routing"))):
    return await routing_service.get_routing_config()


class RoutingConfigIn(BaseModel):
    round_robin_enabled: Optional[bool] = None
    sticky_enabled: Optional[bool] = None
    capacity_enabled: Optional[bool] = None
    max_chats: Optional[int] = None


@router.put("/config")
async def update_config(body: RoutingConfigIn, ctx: dict = Depends(require_tab_permission("routing"))):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return await routing_service.set_routing_config(patch)
