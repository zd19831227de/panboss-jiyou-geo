from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Union
from datetime import datetime


# Brand Models
class BrandBase(BaseModel):
    name: str
    industry: Optional[str] = None
    products: Optional[str] = None
    target_audience: Optional[str] = None
    selling_points: Optional[str] = None
    competitors: Optional[str] = None
    owned_sites: Optional[str] = None
    # 品牌别名，逗号分隔；监测时命中任一别名也算"提到品牌"
    aliases: Optional[str] = None
    target_platforms: Optional[Union[str, List[str]]] = None
    status: Optional[str] = "active"

    @model_validator(mode="before")
    @classmethod
    def coerce_target_platforms(cls, data):
        if isinstance(data, dict):
            tp = data.get("target_platforms")
            if isinstance(tp, list):
                data["target_platforms"] = ",".join(str(x) for x in tp)
        return data


class BrandCreate(BrandBase):
    pass


class BrandUpdate(BaseModel):
    name: Optional[str] = None
    industry: Optional[str] = None
    products: Optional[str] = None
    target_audience: Optional[str] = None
    selling_points: Optional[str] = None
    competitors: Optional[str] = None
    owned_sites: Optional[str] = None
    aliases: Optional[str] = None
    target_platforms: Optional[str] = None
    status: Optional[str] = None


class Brand(BrandBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Keyword Models
class KeywordBase(BaseModel):
    word: str
    category: Optional[str] = None
    search_volume: Optional[int] = None
    ai_status: Optional[str] = "pending"
    ai_platform: Optional[str] = None
    last_tested: Optional[datetime] = None
    notes: Optional[str] = None


class KeywordCreate(KeywordBase):
    brand_id: int


class KeywordUpdate(BaseModel):
    word: Optional[str] = None
    category: Optional[str] = None
    search_volume: Optional[int] = None
    ai_status: Optional[str] = None
    ai_platform: Optional[str] = None
    last_tested: Optional[datetime] = None
    notes: Optional[str] = None


class Keyword(KeywordBase):
    id: int
    brand_id: int
    created_at: datetime

    class Config:
        from_attributes = True


# Monitor Result Models
class MonitorResultBase(BaseModel):
    platform: str
    search_query: str
    is_cited: bool = False
    cited_sources: Optional[str] = None
    screenshot_path: Optional[str] = None
    notes: Optional[str] = None


class MonitorResultCreate(MonitorResultBase):
    brand_id: int
    keyword_id: int


class MonitorResult(MonitorResultBase):
    id: int
    brand_id: int
    keyword_id: int
    monitored_at: datetime

    class Config:
        from_attributes = True


# Monitor Run Request
class MonitorRunRequest(BaseModel):
    brand_id: int
    keyword_ids: List[int]
    platform: str = "doubao"
    run_mode: str = "manual"
    # 每题等待上限（秒）。必须限幅：负值会让 runner 的 deadline 立即过期、
    # 极大值会在队列里长期占住平台（每个平台串行执行）。
    timeout: Optional[int] = Field(default=90, ge=5, le=600)
    auto_rounds: Optional[int] = 1
    scheduled_start_at: Optional[str] = None
    interval_minutes: Optional[int] = None


# Dashboard Response
class DashboardResponse(BaseModel):
    total_keywords: int
    cited_count: int
    citation_rate: float
    monthly_trend: List[dict]
