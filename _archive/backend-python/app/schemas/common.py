"""공통 응답 스키마 (DS-40 §3.4, §4.5)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class OkResponse(BaseModel):
    ok: bool = True
    data: Any = None


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None = None
    has_more: bool = False


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}
