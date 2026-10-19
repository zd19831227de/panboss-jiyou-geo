from fastapi import APIRouter, HTTPException, status, Query
from typing import List, Optional
from urllib.parse import urlparse
from app.database import get_db
from app.models import Brand, BrandCreate, BrandUpdate

router = APIRouter(prefix="/api/brands", tags=["brands"])

ALLOWED_PLATFORMS = {"doubao", "yuanbao", "qianwen", "web"}
LEGACY_PLATFORM_ALIASES = {
    "douyin": "web",
    "xiaohongshu": "web",
    "zhihu": "web",
    "toutiao": "web",
    "sohu": "web",
    "baijiahao": "web",
    "ctrip_travel": "web",
    "website": "web",
}


def _split_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("，", ",").replace("、", ",").split(",") if item.strip()]


def _normalize_aliases(value: Optional[str]) -> str:
    """品牌别名：逗号分隔，去重去空。"""
    seen = []
    for item in _split_csv(value):
        if item not in seen:
            seen.append(item)
    return ",".join(seen)


def _normalize_site_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    if "://" not in value and "." in value and " " not in value:
        normalized = f"https://{value.lstrip('/')}"
        parsed = urlparse(normalized)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return normalized
    return value


def _validate_brand_payload(data):
    name = (getattr(data, "name", None) or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="品牌名称不能为空")

    raw_platforms = _split_csv(getattr(data, "target_platforms", None))
    platforms = []
    for item in raw_platforms:
        normalized = LEGACY_PLATFORM_ALIASES.get(item, item)
        if normalized not in platforms:
            platforms.append(normalized)
    invalid = [item for item in platforms if item not in ALLOWED_PLATFORMS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"目标平台无效：{','.join(invalid)}")

    urls = [_normalize_site_url(item) for item in _split_csv(getattr(data, "owned_sites", None))]
    invalid_urls = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            invalid_urls.append(url)
    if invalid_urls:
        raise HTTPException(status_code=400, detail=f"自有来源必须是 http/https 链接：{','.join(invalid_urls)}")
    return {
        "name": name,
        "target_platforms": ",".join(platforms),
        "owned_sites": ",".join(urls),
    }


@router.get("")
def list_brands():
    """Get all brands with keyword and citation counts"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT
                b.*,
                COUNT(DISTINCT k.id) AS keywordCount,
                (SELECT COUNT(*) FROM monitor_results WHERE brand_id = b.id AND verification_status = 'verified_citation' AND source_verified = 1) AS citationCount,
                (SELECT COUNT(*) FROM monitor_results WHERE brand_id = b.id AND verification_status = 'unverified_citation') AS unverifiedCitationCount,
                (SELECT COUNT(*) FROM monitor_results WHERE brand_id = b.id) AS totalMonitorCount
            FROM brands b
            LEFT JOIN keywords k ON k.brand_id = b.id
            GROUP BY b.id
            ORDER BY b.created_at DESC"""
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


@router.get("/{brand_id}", response_model=Brand)
def get_brand(brand_id: int):
    """Get brand by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Brand not found")
        return dict(row)


@router.post("", response_model=Brand, status_code=status.HTTP_201_CREATED)
def create_brand(brand: BrandCreate):
    """Create a new brand"""
    normalized = _validate_brand_payload(brand)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE name = ?", (normalized["name"],))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="品牌名称已存在")
        cursor.execute(
            """INSERT INTO brands
               (name, industry, products, target_audience, selling_points,
                competitors, owned_sites, aliases, target_platforms, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized["name"],
                (brand.industry or "").strip(),
                (brand.products or "").strip(),
                (brand.target_audience or "").strip(),
                (brand.selling_points or "").strip(),
                (brand.competitors or "").strip(),
                normalized["owned_sites"],
                _normalize_aliases(getattr(brand, "aliases", None)),
                normalized["target_platforms"],
                brand.status,
            )
        )
        brand_id = cursor.lastrowid
        cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        return dict(cursor.fetchone())


@router.put("/{brand_id}", response_model=Brand)
def update_brand(brand_id: int, brand: BrandUpdate):
    """Update a brand"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Brand not found")
        merged = dict(existing)
        for field in [
            "name", "industry", "products", "target_audience", "selling_points",
            "competitors", "owned_sites", "aliases", "target_platforms", "status",
        ]:
            value = getattr(brand, field, None)
            if value is not None:
                merged[field] = value
        class Payload:
            pass
        payload = Payload()
        for key, value in merged.items():
            setattr(payload, key, value)
        normalized = _validate_brand_payload(payload)
        if normalized["name"] != existing["name"]:
            cursor.execute("SELECT id FROM brands WHERE name = ? AND id != ?", (normalized["name"], brand_id))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="品牌名称已存在")

        updates = []
        params = []
        for field in [
            "name", "industry", "products", "target_audience", "selling_points",
            "competitors", "owned_sites", "aliases", "target_platforms", "status",
        ]:
            value = getattr(brand, field, None)
            if value is not None:
                updates.append(f"{field} = ?")
                if field == "name":
                    params.append(normalized["name"])
                elif field == "owned_sites":
                    params.append(normalized["owned_sites"])
                elif field == "target_platforms":
                    params.append(normalized["target_platforms"])
                elif field == "aliases":
                    params.append(_normalize_aliases(value))
                else:
                    params.append(value.strip() if isinstance(value, str) else value)

        if updates:
            params.append(brand_id)
            cursor.execute(
                f"UPDATE brands SET {', '.join(updates)} WHERE id = ?",
                params
            )

        cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        return dict(cursor.fetchone())


@router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand(brand_id: int):
    """Delete a brand"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")
        cursor.execute(
            """DELETE FROM monitor_sources
               WHERE answer_id IN (SELECT id FROM monitor_answers WHERE brand_id = ?)""",
            (brand_id,),
        )
        for table in [
            "monitor_answers",
            "monitor_results",
            "monitor_runs",
            "task_queue",
            "keyword_ideas",
            "content_assets",
            "publishing_tasks",
            "geo_strategy_actions",
            "geo_strategies",
            "geo_reports",
            "monitor_configs",
            "alerts",
            "competitors",
            "keywords",
        ]:
            if table == "task_queue":
                # 不要用文本 LIKE 删 JSON：实测删除 brand_id=1 时，LIKE '..."brand_id": 1%'
                # 因前缀匹配会额外误删 brand_id=11/100 等任务（数据丢失）。
                # 改用 json_extract 精确匹配（SQLite 3.9+ JSON1，本项目已是 3.9+）。
                cursor.execute(
                    "DELETE FROM task_queue WHERE json_extract(params, '$.brand_id') = ?",
                    (brand_id,),
                )
            else:
                cursor.execute(f"DELETE FROM {table} WHERE brand_id = ?", (brand_id,))
        cursor.execute("DELETE FROM brands WHERE id = ?", (brand_id,))


# ─── Brand-specific keyword & citation routes ───

@router.get("/{brand_id}/keywords")
def get_brand_keywords(brand_id: int):
    """Get all keywords for a brand"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM keywords WHERE brand_id = ? ORDER BY created_at DESC",
            (brand_id,)
        )
        return [dict(row) for row in cursor.fetchall()]


@router.get("/{brand_id}/keywords/count")
def get_brand_keywords_count(brand_id: int):
    """Get keyword count for a brand"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM keywords WHERE brand_id = ?",
            (brand_id,)
        )
        row = cursor.fetchone()
        return {"count": row["count"]}


@router.get("/{brand_id}/citations")
def get_brand_citations(brand_id: int):
    """Get citation count for a brand"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND verification_status = 'verified_citation' AND source_verified = 1",
            (brand_id,)
        )
        row = cursor.fetchone()
        return {"count": row["count"]}
