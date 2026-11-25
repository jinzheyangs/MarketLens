"""当前登录主体信息接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.auth.dependencies import get_principal
from app.auth.principal import Principal
from app.core.errors import AuthenticationError
from app.schemas.users import MeResponse
from app.services.users import get_user_by_id, me_response

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeResponse, summary="Current user and memberships")
async def current_principal(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    user = await get_user_by_id(session, principal.user_id)
    if user is None:  # pragma: no cover - principal was loaded from this row
        raise AuthenticationError
    return me_response(principal, user)
