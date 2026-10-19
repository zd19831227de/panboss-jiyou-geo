"""LLM provider settings and usage accounting."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.llm_service import list_providers, test_provider, update_provider, usage_summary


router = APIRouter(prefix="/api/llm", tags=["llm"])


class LlmProviderUpdate(BaseModel):
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    default_model: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=1, le=999)
    prompt_price_per_1k: Optional[float] = Field(default=None, ge=0)
    completion_price_per_1k: Optional[float] = Field(default=None, ge=0)


@router.get("/providers")
def providers():
    return list_providers()


@router.put("/providers/{provider}")
def save_provider(provider: str, payload: LlmProviderUpdate):
    try:
        data = payload.model_dump(exclude_unset=True)
        return update_provider(provider, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/providers/{provider}/test")
def test_connection(provider: str):
    try:
        return test_provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/usage")
def usage():
    return usage_summary()
