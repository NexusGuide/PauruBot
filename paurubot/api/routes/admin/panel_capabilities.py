"""Panel capability report endpoint (F-04)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from paurubot.api.admin_common import owned
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.db.models import Panel
from paurubot.panels import PanelError, registry
from paurubot.panels.capabilities import report

router = APIRouter(prefix="/api/admin", tags=["admin"])
Writer = Annotated[StaffContext, Depends(require("panels.write"))]


class CapabilityBody(BaseModel):
    # Creating a throw-away user on a live panel only happens when staff ask for it explicitly.
    write: bool = False


@router.post("/panels/{panel_id}/capabilities")
async def panel_capabilities(panel_id: int, body: CapabilityBody, ctx: Writer, db: DB) -> dict[str, Any]:
    panel = await owned(db, Panel, panel_id, ctx)
    try:
        driver = await registry.driver_for(panel)
    except PanelError as exc:
        check = {"key": "connect", "status": "fail", "detail": str(exc)[:200]}
        result = {"status": "fail", "write_tested": False, "checks": [check]}
    else:
        result = await report(driver, write=body.write)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.capabilities",
        entity="panel",
        entity_id=panel.id,
        after={"status": result["status"], "write": body.write, "write_tested": result["write_tested"]},
    )
    return result
