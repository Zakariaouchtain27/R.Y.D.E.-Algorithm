"""
Admin API — create/manage agencies and API keys.

Protected by X-Admin-Secret header.
Set RYDE_ADMIN_SECRET in .env. Never expose this value publicly.
"""
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ryde.agency_store import AgencyStore

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="web/templates")

_db_path = os.getenv("RYDE_DB_PATH", "ryde.db")
_agencies = AgencyStore(_db_path)
_ADMIN_SECRET = os.getenv("RYDE_ADMIN_SECRET", "change-me-before-production")


async def require_admin(
    x_admin_secret: Optional[str] = Header(default=None),
) -> None:
    if not x_admin_secret or x_admin_secret != _ADMIN_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin secret.",
        )


def _agency_dict(a) -> dict:
    return {
        "id":           a.id,
        "name":         a.name,
        "email":        a.email,
        "api_key":      a.api_key,
        "environment":  a.environment,
        "active":       a.active,
        "total_calls":  a.total_calls,
        "last_call_at": a.last_call_at,
        "created_at":   a.created_at,
    }


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(request, "admin.html")


class CreateAgencyBody(BaseModel):
    name:        str = Field(..., min_length=2)
    email:       str = Field(...)
    environment: Literal["test", "live"] = "test"


@router.get("/agencies", dependencies=[Depends(require_admin)])
async def list_agencies():
    return [_agency_dict(a) for a in await _agencies.list_agencies()]


@router.post("/agencies", dependencies=[Depends(require_admin)], status_code=201)
async def create_agency(body: CreateAgencyBody):
    agency = await _agencies.create_agency(
        name=body.name,
        email=body.email,
        environment=body.environment,
    )
    return _agency_dict(agency)


@router.delete("/agencies/{agency_id}", dependencies=[Depends(require_admin)])
async def revoke_agency(agency_id: str):
    if not await _agencies.get_by_id(agency_id):
        raise HTTPException(status_code=404, detail="Agency not found.")
    await _agencies.revoke(agency_id)
    return {"ok": True}


@router.post("/agencies/{agency_id}/reactivate", dependencies=[Depends(require_admin)])
async def reactivate_agency(agency_id: str):
    if not await _agencies.get_by_id(agency_id):
        raise HTTPException(status_code=404, detail="Agency not found.")
    await _agencies.reactivate(agency_id)
    return {"ok": True}


@router.post("/agencies/{agency_id}/regenerate", dependencies=[Depends(require_admin)])
async def regenerate_key(agency_id: str):
    agency = await _agencies.regenerate_key(agency_id)
    if not agency:
        raise HTTPException(status_code=404, detail="Agency not found.")
    return {"ok": True, "api_key": agency.api_key}
