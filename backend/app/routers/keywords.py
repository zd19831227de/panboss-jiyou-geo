from fastapi import APIRouter, HTTPException, Query, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from typing import List, Optional
import csv
import io
from app.database import get_db
from app.models import Keyword, KeywordCreate, KeywordUpdate

router = APIRouter(prefix="/api/keywords", tags=["keywords"])

ALLOWED_CATEGORIES = {"brand", "product", "industry", "longtail"}
ALLOWED_AI_STATUSES = {"pending", "cited", "unverified_citation", "not_found"}


def _clean_word(word: Optional[str]) -> str:
    return (word or "").strip()


def _validate_category(category: Optional[str]) -> Optional[str]:
    if not category:
        return None
    category = category.strip()
    if category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
    return category


def _validate_ai_status(ai_status: Optional[str]) -> Optional[str]:
    if not ai_status:
        return None
    ai_status = ai_status.strip()
    if ai_status not in ALLOWED_AI_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid ai_status: {ai_status}")
    return ai_status


def _ensure_brand(cursor, brand_id: int):
    cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Brand not found")


def _keyword_exists(cursor, brand_id: int, word: str, exclude_id: Optional[int] = None) -> bool:
    if exclude_id:
        cursor.execute(
            "SELECT id FROM keywords WHERE brand_id = ? AND word = ? AND id != ?",
            (brand_id, word, exclude_id),
        )
    else:
        cursor.execute("SELECT id FROM keywords WHERE brand_id = ? AND word = ?", (brand_id, word))
    return cursor.fetchone() is not None


def _parse_import_rows(csv_text: str) -> tuple[list[list[str]], dict[str, int]]:
    reader = csv.reader(io.StringIO(csv_text))
    rows = [row for row in reader if row and any((cell or "").strip() for cell in row)]
    if not rows:
        return [], {}

    first = [(cell or "").strip().lower() for cell in rows[0]]
    aliases = {
        "word": {"word", "keyword", "关键词"},
        "category": {"category", "分类"},
        "search_volume": {"search_volume", "volume", "搜索量"},
        "brand_id": {"brand_id", "品牌id", "品牌ID".lower()},
    }
    has_header = any(cell in aliases["word"] for cell in first) or any(cell in aliases["brand_id"] for cell in first)
    header_map: dict[str, int] = {}
    if has_header:
        for idx, name in enumerate(first):
            for field, names in aliases.items():
                if name in names:
                    header_map[field] = idx
        return rows[1:], header_map
    return rows, {"word": 0, "category": 1, "search_volume": 2, "brand_id": 3}


def _cell(row: list[str], header_map: dict[str, int], field: str) -> str:
    idx = header_map.get(field)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _import_keyword_rows(cursor, rows: list[list[str]], header_map: dict[str, int], default_brand_id: Optional[int]):
    keywords = []
    skipped = []
    for row in rows:
        word = _clean_word(_cell(row, header_map, "word"))
        if not word:
            skipped.append({"row": row, "reason": "关键词为空"})
            continue
        try:
            category = _validate_category(_cell(row, header_map, "category") or None)
        except HTTPException as exc:
            skipped.append({"word": word, "reason": exc.detail})
            continue
        volume_text = _cell(row, header_map, "search_volume")
        search_volume = int(volume_text) if volume_text.isdigit() else None

        bid = default_brand_id
        csv_brand_id = _cell(row, header_map, "brand_id")
        if csv_brand_id.isdigit():
            bid = int(csv_brand_id)
        if bid is None:
            skipped.append({"word": word, "reason": "缺少 brand_id"})
            continue

        cursor.execute("SELECT id FROM brands WHERE id = ?", (bid,))
        if not cursor.fetchone():
            skipped.append({"word": word, "reason": f"品牌不存在: {bid}"})
            continue
        if _keyword_exists(cursor, bid, word):
            skipped.append({"word": word, "reason": "重复关键词"})
            continue

        cursor.execute(
            """INSERT INTO keywords (brand_id, word, category, search_volume)
               VALUES (?, ?, ?, ?)""",
            (bid, word, category, search_volume),
        )
        keyword_id = cursor.lastrowid
        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        keywords.append(dict(cursor.fetchone()))
    return keywords, skipped


@router.get("")
def list_keywords(
    brand_id: Optional[int] = Query(None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=20, ge=1, le=200),
    category: Optional[str] = Query(None),
    ai_status: Optional[str] = Query(None),
):
    """Get all keywords, optionally filtered by brand_id. Supports pagination."""
    with get_db() as conn:
        cursor = conn.cursor()

        where_clauses = []
        params = []

        if brand_id:
            where_clauses.append("k.brand_id = ?")
            params.append(brand_id)
        if category:
            category = _validate_category(category)
            where_clauses.append("k.category = ?")
            params.append(category)
        if ai_status:
            ai_status = _validate_ai_status(ai_status)
            where_clauses.append("k.ai_status = ?")
            params.append(ai_status)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get total count
        cursor.execute(
            f"SELECT COUNT(*) as total FROM keywords k {where_sql}", params
        )
        total = cursor.fetchone()["total"]

        # Get paginated results
        if page is not None:
            offset = (page - 1) * page_size
            cursor.execute(
                f"SELECT k.* FROM keywords k {where_sql} ORDER BY k.created_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            )
        else:
            cursor.execute(
                f"SELECT k.* FROM keywords k {where_sql} ORDER BY k.created_at DESC",
                params,
            )

        rows = cursor.fetchall()
        return {"items": [dict(row) for row in rows], "total": total}


@router.get("/stats")
def keyword_stats(
    brand_id: Optional[int] = Query(None),
    category: Optional[str] = Query(None),
    ai_status: Optional[str] = Query(None),
):
    """关键词页全量统计，按当前筛选条件计算，避免用分页数据误导。"""
    with get_db() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        if brand_id:
            where.append("brand_id = ?")
            params.append(brand_id)
        if category:
            category = _validate_category(category)
            where.append("category = ?")
            params.append(category)
        if ai_status:
            ai_status = _validate_ai_status(ai_status)
            where.append("ai_status = ?")
            params.append(ai_status)
        where_sql = "WHERE " + " AND ".join(where) if where else ""

        cursor.execute(
            f"""SELECT COALESCE(ai_platform, 'pending') AS platform,
                       COUNT(*) AS total,
                       SUM(CASE WHEN ai_status = 'cited' THEN 1 ELSE 0 END) AS cited,
                       SUM(CASE WHEN ai_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                       SUM(CASE WHEN ai_status = 'not_found' THEN 1 ELSE 0 END) AS not_found,
                       SUM(CASE WHEN ai_status = 'pending' OR ai_status IS NULL THEN 1 ELSE 0 END) AS pending
                FROM keywords
                {where_sql}
                GROUP BY COALESCE(ai_platform, 'pending')
                ORDER BY total DESC""",
            params,
        )
        by_platform = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(CASE WHEN ai_status = 'cited' THEN 1 ELSE 0 END) AS cited,
                       SUM(CASE WHEN ai_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                       SUM(CASE WHEN ai_status = 'not_found' THEN 1 ELSE 0 END) AS not_found,
                       SUM(CASE WHEN ai_status = 'pending' OR ai_status IS NULL THEN 1 ELSE 0 END) AS pending
                FROM keywords
                {where_sql}""",
            params,
        )
        total = dict(cursor.fetchone())
        return {"total": total, "by_platform": by_platform}


@router.get("/export_csv")
def export_keywords(brand_id: Optional[int] = Query(None), category: Optional[str] = Query(None)):
    """Export keywords to CSV"""
    with get_db() as conn:
        cursor = conn.cursor()
        where = []
        params = []
        if brand_id:
            where.append("brand_id = ?")
            params.append(brand_id)
        if category:
            category = _validate_category(category)
            where.append("category = ?")
            params.append(category)

        where_sql = ""
        if where:
            where_sql = "WHERE " + " AND ".join(where)

        cursor.execute(
            f"SELECT word, category, search_volume, ai_status, ai_platform, last_tested, notes FROM keywords {where_sql}",
            params,
        )
        rows = cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["word", "category", "search_volume", "ai_status", "ai_platform", "last_tested", "notes"])
        for row in rows:
            writer.writerow(list(row))

        output.seek(0)
        filename = f"keywords_{brand_id or 'all'}_{category or 'all'}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )


@router.get("/{keyword_id}", response_model=Keyword)
def get_keyword(keyword_id: int):
    """Get keyword by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Keyword not found")
        return dict(row)


@router.post("/import_csv", status_code=status.HTTP_201_CREATED)
async def import_csv_legacy(brand_id: int = Query(...), file: UploadFile = File(...)):
    """Import keywords from CSV file for a specific brand (legacy endpoint)"""
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")
    content = await file.read()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")

        rows, header_map = _parse_import_rows(content.decode("utf-8-sig"))
        keywords, skipped = _import_keyword_rows(cursor, rows, header_map, brand_id)
        return {"items": keywords, "total": len(keywords), "skipped": skipped, "skipped_total": len(skipped)}


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_keywords(
    file: UploadFile = File(...),
    brand_id: Optional[int] = Form(None),
):
    """Import keywords from CSV file via multipart form upload.
    CSV format: word, category, search_volume, brand_id (optional header row).
    If brand_id is not provided, expects a 'brand_id' column in CSV or uses query param.
    """
    content = await file.read()
    csv_text = content.decode("utf-8-sig")

    with get_db() as conn:
        cursor = conn.cursor()

        rows, header_map = _parse_import_rows(csv_text)
        keywords, skipped = _import_keyword_rows(cursor, rows, header_map, brand_id)

        return {"items": keywords, "total": len(keywords), "skipped": skipped, "skipped_total": len(skipped)}


@router.post("", response_model=Keyword, status_code=status.HTTP_201_CREATED)
def create_keyword(keyword: KeywordCreate):
    """Create a new keyword"""
    word = _clean_word(keyword.word)
    if not word:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    category = _validate_category(keyword.category)
    ai_status = _validate_ai_status(keyword.ai_status)
    with get_db() as conn:
        cursor = conn.cursor()
        _ensure_brand(cursor, keyword.brand_id)
        if _keyword_exists(cursor, keyword.brand_id, word):
            raise HTTPException(status_code=409, detail="关键词已存在")

        cursor.execute(
            """INSERT INTO keywords
               (brand_id, word, category, search_volume, ai_status, ai_platform, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (keyword.brand_id, word, category, keyword.search_volume,
             ai_status, keyword.ai_platform, keyword.notes)
        )
        keyword_id = cursor.lastrowid
        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        return dict(cursor.fetchone())


@router.put("/{keyword_id}", response_model=Keyword)
def update_keyword(keyword_id: int, keyword: KeywordUpdate):
    """Update a keyword"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Keyword not found")

        updates = []
        params = []
        for field in ["word", "category", "search_volume", "ai_status", "ai_platform", "last_tested", "notes"]:
            value = getattr(keyword, field, None)
            if value is not None:
                if field == "word":
                    value = _clean_word(value)
                    if not value:
                        raise HTTPException(status_code=400, detail="关键词不能为空")
                    if _keyword_exists(cursor, existing["brand_id"], value, keyword_id):
                        raise HTTPException(status_code=409, detail="关键词已存在")
                if field == "category":
                    value = _validate_category(value)
                if field == "ai_status":
                    value = _validate_ai_status(value)
                updates.append(f"{field} = ?")
                params.append(value)

        if updates:
            params.append(keyword_id)
            cursor.execute(
                f"UPDATE keywords SET {', '.join(updates)} WHERE id = ?",
                params
            )

        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        return dict(cursor.fetchone())


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_keyword(keyword_id: int):
    """Delete a keyword"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM keywords WHERE id = ?", (keyword_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Keyword not found")
        cursor.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))
