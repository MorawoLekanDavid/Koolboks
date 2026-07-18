from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx
from chatbot.models import Lead, LeadAssignmentRule

router = APIRouter(prefix="/admin/assignment-rules", tags=["assignment"])

VALID_FIELDS = {"product_interest", "business", "status", "any"}
VALID_OPS = {"contains", "equals", "is_empty", "is_not_empty", "any"}


class RuleIn(BaseModel):
    condition_field: str
    condition_operator: str
    condition_value: Optional[str] = None
    assign_to: str
    priority: int = 0


@router.get("")
async def list_rules(ctx: dict = Depends(get_admin_ctx)):
    def _fetch():
        db = get_db()
        try:
            rules = (
                db.query(LeadAssignmentRule)
                .filter(LeadAssignmentRule.active == True)
                .order_by(LeadAssignmentRule.priority, LeadAssignmentRule.id)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "condition_field": r.condition_field,
                    "condition_operator": r.condition_operator,
                    "condition_value": r.condition_value,
                    "assign_to": r.assign_to,
                    "priority": r.priority,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rules
            ]
        finally:
            db.close()
    return await run_in_threadpool(_fetch)


@router.post("")
async def create_rule(body: RuleIn, ctx: dict = Depends(get_admin_ctx)):
    if body.condition_field not in VALID_FIELDS:
        raise HTTPException(400, f"Invalid condition_field. Valid: {', '.join(sorted(VALID_FIELDS))}")
    if body.condition_operator not in VALID_OPS:
        raise HTTPException(400, f"Invalid condition_operator. Valid: {', '.join(sorted(VALID_OPS))}")
    if body.condition_operator not in ("is_empty", "is_not_empty", "any") and body.condition_field != "any":
        if not body.condition_value or not body.condition_value.strip():
            raise HTTPException(400, "condition_value required for this operator")
    if not body.assign_to.strip():
        raise HTTPException(400, "assign_to is required")

    def _create():
        db = get_db()
        try:
            rule = LeadAssignmentRule(
                condition_field=body.condition_field,
                condition_operator=body.condition_operator,
                condition_value=body.condition_value.strip() if body.condition_value else None,
                assign_to=body.assign_to.strip(),
                priority=body.priority,
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            return {
                "id": rule.id,
                "condition_field": rule.condition_field,
                "condition_operator": rule.condition_operator,
                "condition_value": rule.condition_value,
                "assign_to": rule.assign_to,
                "priority": rule.priority,
            }
        finally:
            db.close()
    return await run_in_threadpool(_create)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, ctx: dict = Depends(get_admin_ctx)):
    def _delete():
        db = get_db()
        try:
            rule = db.query(LeadAssignmentRule).filter(LeadAssignmentRule.id == rule_id).first()
            if not rule:
                raise HTTPException(404, "Rule not found")
            db.delete(rule)
            db.commit()
            return {"ok": True}
        finally:
            db.close()
    return await run_in_threadpool(_delete)


def _rule_matches(lead: Lead, rule: LeadAssignmentRule) -> bool:
    if rule.condition_field == "any" or rule.condition_operator == "any":
        return True
    raw = getattr(lead, rule.condition_field, None)
    v = (raw or "").lower().strip()
    cv = (rule.condition_value or "").lower().strip()
    if rule.condition_operator == "equals":
        return v == cv
    if rule.condition_operator == "contains":
        return bool(v) and cv in v
    if rule.condition_operator == "is_empty":
        return not v
    if rule.condition_operator == "is_not_empty":
        return bool(v)
    return False


@router.post("/run")
async def run_auto_assign(ctx: dict = Depends(get_admin_ctx)):
    def _run():
        db = get_db()
        try:
            rules = (
                db.query(LeadAssignmentRule)
                .filter(LeadAssignmentRule.active == True)
                .order_by(LeadAssignmentRule.priority, LeadAssignmentRule.id)
                .all()
            )
            if not rules:
                return {"assigned": 0, "message": "No active rules configured"}
            unassigned = db.query(Lead).filter(Lead.assigned_to == None).all()
            count = 0
            for lead in unassigned:
                for rule in rules:
                    if _rule_matches(lead, rule):
                        lead.assigned_to = rule.assign_to
                        count += 1
                        break
            db.commit()
            return {"assigned": count, "message": f"Auto-assigned {count} lead(s)"}
        finally:
            db.close()
    return await run_in_threadpool(_run)
