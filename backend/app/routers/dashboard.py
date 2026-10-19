"""Dashboard router — 完整数据看板"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from app.database import get_db
from app.services.evidence import build_evidence_web_url
from app.services.platform_adapters.sessions import check_all_platform_sessions

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ALLOWED_MONITOR_PLATFORMS = "'web','doubao','qianwen','yuanbao'"


def _pct(part, total):
    return round((part or 0) / total * 100, 2) if total else 0.0


@router.get("/command-center")
def get_command_center(brand_id: Optional[int] = Query(default=None)):
    """GEO 运营大屏：只汇总真实监测数据。"""
    with get_db() as conn:
        cursor = conn.cursor()
        brand_filter = ""
        brand_params: list[int] = []
        if brand_id:
            cursor.execute("SELECT id, name FROM brands WHERE id = ?", (brand_id,))
            brand_row = cursor.fetchone()
            if not brand_row:
                raise HTTPException(status_code=404, detail="Brand not found")
            brand_filter = " AND brand_id = ?"
            brand_params = [brand_id]
        else:
            brand_row = None

        cursor.execute(
            "SELECT COUNT(*) AS count FROM brands WHERE status = 'active'" + (" AND id = ?" if brand_id else ""),
            tuple(brand_params),
        )
        active_brands = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM keywords WHERE 1=1" + brand_filter, tuple(brand_params))
        total_keywords = cursor.fetchone()["count"] or 0

        cursor.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN is_cited = 1 THEN 1 ELSE 0 END) AS cited,
                      SUM(CASE WHEN verification_status = 'verified_citation' AND source_verified = 1 THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN verification_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                      SUM(CASE WHEN verification_status = 'not_cited' THEN 1 ELSE 0 END) AS not_cited,
                      SUM(CASE WHEN verification_status = 'failed' OR (failure_reason IS NOT NULL AND failure_reason != '') THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN source_verified = 1 THEN 1 ELSE 0 END) AS with_source,
                      SUM(CASE WHEN screenshot_verified = 1 THEN 1 ELSE 0 END) AS with_screenshot,
                      SUM(CASE WHEN answer_text IS NOT NULL AND answer_text != '' THEN 1 ELSE 0 END) AS with_answer
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)""" + brand_filter,
            tuple(brand_params),
        )
        overview = dict(cursor.fetchone())
        total_monitor = overview.get("total") or 0

        cursor.execute(
            """SELECT platform,
                      COUNT(*) AS total,
                      SUM(CASE WHEN verification_status = 'verified_citation' AND source_verified = 1 THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN verification_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                      SUM(CASE WHEN is_cited = 1 THEN 1 ELSE 0 END) AS cited,
                      SUM(CASE WHEN source_verified = 1 THEN 1 ELSE 0 END) AS with_source,
                      SUM(CASE WHEN failure_reason IS NOT NULL AND failure_reason != '' THEN 1 ELSE 0 END) AS failed,
                      AVG(CASE WHEN elapsed_seconds IS NOT NULL THEN elapsed_seconds ELSE NULL END) AS avg_elapsed
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)""" + brand_filter + """
               GROUP BY platform
               ORDER BY total DESC""",
            tuple(brand_params),
        )
        platform_stats = []
        for row in cursor.fetchall():
            item = dict(row)
            item["verified_rate"] = _pct(item.get("verified"), item.get("total") or 0)
            item["source_rate"] = _pct(item.get("with_source"), item.get("total") or 0)
            item["avg_elapsed"] = round(item.get("avg_elapsed") or 0, 2)
            platform_stats.append(item)

        cursor.execute(
            """SELECT label,
                      SUM(total) AS total,
                      SUM(verified) AS verified,
                      SUM(failed) AS failed
               FROM (
                    SELECT date(monitored_at) AS label,
                           COUNT(*) AS total,
                           SUM(CASE WHEN verification_status = 'verified_citation' AND source_verified = 1 THEN 1 ELSE 0 END) AS verified,
                           SUM(CASE WHEN verification_status = 'failed' OR (failure_reason IS NOT NULL AND failure_reason != '') THEN 1 ELSE 0 END) AS failed
                    FROM monitor_results
                    WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)""" + brand_filter + """
                    GROUP BY date(monitored_at)
               )
               GROUP BY label
               ORDER BY label DESC
               LIMIT 14""",
            tuple(brand_params),
        )
        daily_trend = [dict(row) for row in cursor.fetchall()][::-1]

        cursor.execute(
            """SELECT domain, COUNT(*) AS count
               FROM monitor_sources ms
               JOIN monitor_answers ma ON ma.id = ms.answer_id
               WHERE ms.domain IS NOT NULL AND ms.domain != '' AND ms.source_verified = 1""" + (" AND ma.brand_id = ?" if brand_id else "") + """
               GROUP BY domain
               ORDER BY count DESC
               LIMIT 10""",
            tuple(brand_params),
        )
        source_domains = [dict(row) for row in cursor.fetchall()]
        if not source_domains:
            cursor.execute(
                """SELECT source_domain AS domain, COUNT(*) AS count
                   FROM monitor_results
                   WHERE source_domain IS NOT NULL AND source_domain != ''""" + brand_filter + """
                   GROUP BY source_domain
                   ORDER BY count DESC
                   LIMIT 10""",
                tuple(brand_params),
            )
            source_domains = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            """SELECT COALESCE(NULLIF(failure_reason, ''), '未知失败') AS reason,
                      platform,
                      COUNT(*) AS count
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)""" + brand_filter + """
                 AND (verification_status = 'failed' OR (failure_reason IS NOT NULL AND failure_reason != ''))
               GROUP BY reason, platform
               ORDER BY count DESC
               LIMIT 10""",
            tuple(brand_params),
        )
        failure_reasons = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            """SELECT b.id, b.name, b.industry,
                      COUNT(mr.id) AS total,
                      SUM(CASE WHEN mr.verification_status = 'verified_citation' AND mr.source_verified = 1 THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN mr.verification_status = 'unverified_citation' THEN 1 ELSE 0 END) AS unverified,
                      SUM(CASE WHEN mr.source_verified = 1 THEN 1 ELSE 0 END) AS with_source,
                      SUM(CASE WHEN mr.failure_reason IS NOT NULL AND mr.failure_reason != '' THEN 1 ELSE 0 END) AS failed,
                      (SELECT COUNT(*) FROM keywords k WHERE k.brand_id = b.id) AS keywords
               FROM brands b
               LEFT JOIN monitor_results mr ON mr.brand_id = b.id AND mr.platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               """ + ("WHERE b.id = ?" if brand_id else "") + """
               GROUP BY b.id
               ORDER BY verified DESC, total DESC
               LIMIT 10""",
            tuple(brand_params),
        )
        brand_ranking = []
        for row in cursor.fetchall():
            item = dict(row)
            item["verified_rate"] = _pct(item.get("verified"), item.get("total") or 0)
            item["evidence_rate"] = _pct(item.get("with_source"), item.get("total") or 0)
            brand_ranking.append(item)

        cursor.execute(
            """SELECT mr.id, mr.platform, mr.search_query, mr.is_cited,
                      mr.verification_status, mr.source_url, mr.source_domain,
                      mr.source_verified, mr.screenshot_verified, mr.raw_artifact_verified,
                      mr.failure_reason, mr.monitored_at,
                      mr.screenshot_path, mr.raw_artifact_path,
                      b.name AS brand_name, k.word AS keyword
               FROM monitor_results mr
               LEFT JOIN brands b ON mr.brand_id = b.id
               LEFT JOIN keywords k ON mr.keyword_id = k.id
               WHERE mr.platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)""" + (" AND mr.brand_id = ?" if brand_id else "") + """
               ORDER BY mr.monitored_at DESC, mr.id DESC
               LIMIT 12""",
            tuple(brand_params),
        )
        latest_results = []
        for row in cursor.fetchall():
            item = dict(row)
            item["screenshot_url"] = build_evidence_web_url(item.get("screenshot_path"))
            item["raw_artifact_url"] = build_evidence_web_url(item.get("raw_artifact_path"))
            latest_results.append(item)

        cursor.execute(
            """SELECT id, brand_id, platforms, status, total_operations,
                      success_count, failed_count, verified_citation_count,
                      unverified_citation_count, business_status, business_failure_reason,
                      effective_source_count, brand_mention_count, brand_mention_rate,
                      created_at, completed_at
               FROM monitor_runs
               WHERE 1=1""" + brand_filter + """
               ORDER BY created_at DESC, id DESC
               LIMIT 8""",
            tuple(brand_params),
        )
        latest_runs = [dict(row) for row in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) AS count FROM keyword_ideas WHERE 1=1" + brand_filter, tuple(brand_params))
        keyword_ideas = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM content_assets WHERE status != 'rejected'" + brand_filter, tuple(brand_params))
        content_assets = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM content_assets WHERE status = 'rejected'" + brand_filter, tuple(brand_params))
        rejected_content_assets = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM publishing_tasks WHERE 1=1" + brand_filter, tuple(brand_params))
        publishing_tasks = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM publishing_tasks WHERE status = 'failed'" + brand_filter, tuple(brand_params))
        historical_failed_publish_tasks = cursor.fetchone()["count"] or 0
        cursor.execute(
            """SELECT COUNT(*) AS count
               FROM publishing_tasks
               WHERE status IN ('pending','prepared','running','manual_required','login_required','published')""" + brand_filter,
            tuple(brand_params),
        )
        current_publish_tasks = cursor.fetchone()["count"] or 0
        cursor.execute(
            """SELECT COUNT(*) AS count
               FROM publishing_tasks
               WHERE status = 'published' AND published_url_verified = 1 AND evidence_status = 'verified'""" + brand_filter,
            tuple(brand_params),
        )
        verified_publish_tasks = cursor.fetchone()["count"] or 0
        cursor.execute("SELECT COUNT(*) AS count FROM publishing_tasks WHERE status IN ('prepared','published')" + brand_filter, tuple(brand_params))
        prepared_publish_tasks = cursor.fetchone()["count"] or 0
        publish_ready_rate = _pct(prepared_publish_tasks, current_publish_tasks)
        cursor.execute(
            """SELECT status, COUNT(*) AS count
               FROM publishing_tasks
               WHERE 1=1""" + brand_filter + """
               GROUP BY status
               ORDER BY count DESC""",
            tuple(brand_params),
        )
        publishing_status = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """SELECT pt.id, pt.platform, pt.status, pt.published_url,
                      pt.screenshot_path, pt.raw_artifact_path, pt.failure_reason,
                      pt.updated_at, b.name AS brand_name, ca.title
               FROM publishing_tasks pt
               LEFT JOIN brands b ON b.id = pt.brand_id
               LEFT JOIN content_assets ca ON ca.id = pt.content_id
               WHERE 1=1""" + (" AND pt.brand_id = ?" if brand_id else "") + """
               ORDER BY pt.updated_at DESC, pt.id DESC
               LIMIT 12""",
            tuple(brand_params),
        )
        latest_publish_tasks = []
        for row in cursor.fetchall():
            item = dict(row)
            item["screenshot_url"] = build_evidence_web_url(item.get("screenshot_path"))
            item["raw_artifact_url"] = build_evidence_web_url(item.get("raw_artifact_path"))
            latest_publish_tasks.append(item)
        cursor.execute("SELECT COUNT(*) AS count FROM geo_reports WHERE 1=1" + brand_filter, tuple(brand_params))
        geo_reports = cursor.fetchone()["count"] or 0

    platform_sessions = check_all_platform_sessions()
    usable_platforms = sum(1 for item in platform_sessions.values() if item.get("status") == "logged_in")

    return {
        "scope": {
            "brand_id": brand_id,
            "brand_name": brand_row["name"] if brand_row else "全部品牌",
        },
        "overview": {
            "active_brands": active_brands,
            "total_keywords": total_keywords,
            "total_monitor": total_monitor,
            "cited": overview.get("cited") or 0,
            "verified": overview.get("verified") or 0,
            "unverified": overview.get("unverified") or 0,
            "not_cited": overview.get("not_cited") or 0,
            "failed": overview.get("failed") or 0,
            "with_source": overview.get("with_source") or 0,
            "with_screenshot": overview.get("with_screenshot") or 0,
            "with_answer": overview.get("with_answer") or 0,
            "business_failed_runs": sum(1 for row in latest_runs if row.get("business_status") == "failed_quality_gate"),
            "verified_rate": _pct(overview.get("verified"), total_monitor),
            "citation_rate": _pct(overview.get("cited"), total_monitor),
            "evidence_rate": _pct(overview.get("with_source"), total_monitor),
            "usable_platforms": usable_platforms,
            "platform_count": len(platform_sessions),
            "keyword_ideas": keyword_ideas,
            "content_assets": content_assets,
            "rejected_content_assets": rejected_content_assets,
            "publishing_tasks": publishing_tasks,
            "current_publish_tasks": current_publish_tasks,
            "historical_failed_publish_tasks": historical_failed_publish_tasks,
            "verified_publish_tasks": verified_publish_tasks,
            "prepared_publish_tasks": prepared_publish_tasks,
            "publish_ready_rate": publish_ready_rate,
            "geo_reports": geo_reports,
        },
        "platform_sessions": platform_sessions,
        "platform_stats": platform_stats,
        "daily_trend": daily_trend,
        "source_domains": source_domains,
        "failure_reasons": failure_reasons,
        "brand_ranking": brand_ranking,
        "latest_results": latest_results,
        "latest_runs": latest_runs,
        "publishing_status": publishing_status,
        "latest_publish_tasks": latest_publish_tasks,
    }


@router.get("/all-stats")
def get_all_dashboard_stats():
    """全品牌汇总统计"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM keywords")
        total_keywords = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM content_assets WHERE status != 'rejected'")
        content_asset_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM publishing_tasks")
        publishing_task_count = cursor.fetchone()["count"]

        cursor.execute(
            """SELECT COUNT(*) as count
               FROM publishing_tasks
               WHERE status = 'published' AND published_url_verified = 1"""
        )
        verified_published_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND is_cited = 1")
        citation_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND verification_status = 'verified_citation' AND source_verified = 1")
        verified_citation_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND verification_status = 'unverified_citation'")
        unverified_citation_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ")")
        total_monitor = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND source_verified = 1")
        with_source_url = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND screenshot_verified = 1")
        with_screenshot = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_results WHERE platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND answer_text IS NOT NULL AND answer_text != ''")
        with_answer_text = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM monitor_runs WHERE business_status = 'failed_quality_gate'")
        business_failed_runs = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(DISTINCT id) as count FROM brands WHERE status = 'active'")
        active_brands = cursor.fetchone()["count"]

        citation_rate = round((verified_citation_count / total_monitor * 100) if total_monitor > 0 else 0.0, 2)

        return {
            "totalKeywords": total_keywords,
            "contentAssetCount": content_asset_count,
            "publishingTaskCount": publishing_task_count,
            "verifiedPublishedCount": verified_published_count,
            "publishedContent": content_asset_count,
            "citationCount": citation_count,
            "verifiedCitationCount": verified_citation_count,
            "unverifiedCitationCount": unverified_citation_count,
            "citationRate": citation_rate,
            "activeBrands": active_brands,
            "totalMonitorCount": total_monitor,
            "withSourceUrl": with_source_url,
            "withScreenshot": with_screenshot,
            "withAnswerText": with_answer_text,
            "businessFailedRuns": business_failed_runs,
            "evidenceCompleteRate": round((verified_citation_count / total_monitor * 100) if total_monitor > 0 else 0.0, 2),
        }


@router.get("/all-monthly-trend")
def get_all_monthly_trend():
    """全品牌月度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT strftime('%Y-%m', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-%m', monitored_at)
               ORDER BY label DESC LIMIT 6"""
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/all-weekly-trend")
def get_all_weekly_trend():
    """全品牌周度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT strftime('%Y-W%W', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-W%W', monitored_at)
               ORDER BY label DESC LIMIT 6"""
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/all-daily-trend")
def get_all_daily_trend():
    """全品牌日度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT strftime('%Y-%m-%d', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-%m-%d', monitored_at)
               ORDER BY label DESC LIMIT 14"""
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/all-recent-results")
def get_all_recent_results(limit: int = Query(default=10)):
    """全品牌最近监测结果"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT k.word as keyword, m.platform, m.is_cited,
                      m.verification_status, m.source_url, m.source_domain,
                      m.screenshot_path, m.raw_artifact_path, m.failure_reason,
                      m.source_verified, m.screenshot_verified, m.raw_artifact_verified,
                      m.answer_text, m.notes,
                      b.name as brand_name, m.monitored_at
               FROM monitor_results m
               JOIN keywords k ON m.keyword_id = k.id
               JOIN brands b ON m.brand_id = b.id
               WHERE m.platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               ORDER BY m.monitored_at DESC LIMIT ?""",
            (limit,)
        )
        return [
            {
                "keyword": row["keyword"],
                "platform": row["platform"],
                "is_cited": bool(row["is_cited"]),
                "verification_status": row["verification_status"],
                "source_url": row["source_url"],
                "source_domain": row["source_domain"],
                "screenshot_path": row["screenshot_path"],
                "raw_artifact_path": row["raw_artifact_path"],
                "source_verified": row["source_verified"],
                "screenshot_verified": row["screenshot_verified"],
                "raw_artifact_verified": row["raw_artifact_verified"],
                "failure_reason": row["failure_reason"],
                "answer_text": row["answer_text"],
                "notes": row["notes"],
                "brand_name": row["brand_name"],
                "monitored_at": row["monitored_at"],
            }
            for row in cursor.fetchall()
        ]


@router.get("/stats")
def get_dashboard_stats(brand_id: int = Query(...)):
    """品牌统计"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")

        cursor.execute("SELECT COUNT(*) as count FROM keywords WHERE brand_id = ?", (brand_id,))
        total_keywords = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM content_assets WHERE brand_id = ? AND status != 'rejected'",
            (brand_id,),
        )
        content_asset_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM publishing_tasks WHERE brand_id = ?",
            (brand_id,),
        )
        publishing_task_count = cursor.fetchone()["count"]

        cursor.execute(
            """SELECT COUNT(*) as count
               FROM publishing_tasks
               WHERE brand_id = ? AND status = 'published' AND published_url_verified = 1""",
            (brand_id,),
        )
        verified_published_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND is_cited = 1",
            (brand_id,)
        )
        citation_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND verification_status = 'verified_citation' AND source_verified = 1",
            (brand_id,)
        )
        verified_citation_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND verification_status = 'unverified_citation'",
            (brand_id,)
        )
        unverified_citation_count = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ")",
            (brand_id,)
        )
        total_monitor = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND source_verified = 1",
            (brand_id,)
        )
        with_source_url = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND screenshot_verified = 1",
            (brand_id,)
        )
        with_screenshot = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_results WHERE brand_id = ? AND platform IN (" + ALLOWED_MONITOR_PLATFORMS + ") AND answer_text IS NOT NULL AND answer_text != ''",
            (brand_id,)
        )
        with_answer_text = cursor.fetchone()["count"]

        cursor.execute(
            "SELECT COUNT(*) as count FROM monitor_runs WHERE brand_id = ? AND business_status = 'failed_quality_gate'",
            (brand_id,),
        )
        business_failed_runs = cursor.fetchone()["count"]

        citation_rate = round((verified_citation_count / total_monitor * 100) if total_monitor > 0 else 0.0, 2)

        return {
            "totalKeywords": total_keywords,
            "contentAssetCount": content_asset_count,
            "publishingTaskCount": publishing_task_count,
            "verifiedPublishedCount": verified_published_count,
            "publishedContent": content_asset_count,
            "citationCount": citation_count,
            "verifiedCitationCount": verified_citation_count,
            "unverifiedCitationCount": unverified_citation_count,
            "citationRate": citation_rate,
            "totalMonitorCount": total_monitor,
            "withSourceUrl": with_source_url,
            "withScreenshot": with_screenshot,
            "withAnswerText": with_answer_text,
            "businessFailedRuns": business_failed_runs,
            "evidenceCompleteRate": round((verified_citation_count / total_monitor * 100) if total_monitor > 0 else 0.0, 2),
        }


@router.get("/monthly-trend")
def get_monthly_trend(brand_id: int = Query(...)):
    """品牌月度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")

        cursor.execute(
            """SELECT strftime('%Y-%m', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE brand_id = ? AND platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-%m', monitored_at)
               ORDER BY label DESC LIMIT 6""",
            (brand_id,)
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/weekly-trend")
def get_weekly_trend(brand_id: int = Query(...)):
    """品牌周度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")

        cursor.execute(
            """SELECT strftime('%Y-W%W', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE brand_id = ? AND platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-W%W', monitored_at)
               ORDER BY label DESC LIMIT 6""",
            (brand_id,)
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/daily-trend")
def get_daily_trend(brand_id: int = Query(...)):
    """品牌日度趋势"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM brands WHERE id = ?", (brand_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Brand not found")

        cursor.execute(
            """SELECT strftime('%Y-%m-%d', monitored_at) as label,
                      COUNT(*) as count
               FROM monitor_results
               WHERE brand_id = ? AND platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               GROUP BY strftime('%Y-%m-%d', monitored_at)
               ORDER BY label DESC LIMIT 14""",
            (brand_id,)
        )
        return [{"label": row["label"], "count": row["count"]} for row in cursor.fetchall()]


@router.get("/recent-results")
def get_recent_results(brand_id: int = Query(...), limit: int = Query(default=10)):
    """品牌最近结果"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT k.word as keyword, m.platform, m.is_cited,
                      m.verification_status, m.source_url, m.source_domain,
                      m.screenshot_path, m.raw_artifact_path, m.failure_reason,
                      m.source_verified, m.screenshot_verified, m.raw_artifact_verified,
                      m.answer_text, m.notes, m.monitored_at
               FROM monitor_results m
               JOIN keywords k ON m.keyword_id = k.id
               WHERE m.brand_id = ? AND m.platform IN (""" + ALLOWED_MONITOR_PLATFORMS + """)
               ORDER BY m.monitored_at DESC LIMIT ?""",
            (brand_id, limit)
        )
        return [
            {
                "keyword": row["keyword"],
                "platform": row["platform"],
                "is_cited": bool(row["is_cited"]),
                "verification_status": row["verification_status"],
                "source_url": row["source_url"],
                "source_domain": row["source_domain"],
                "screenshot_path": row["screenshot_path"],
                "raw_artifact_path": row["raw_artifact_path"],
                "source_verified": row["source_verified"],
                "screenshot_verified": row["screenshot_verified"],
                "raw_artifact_verified": row["raw_artifact_verified"],
                "failure_reason": row["failure_reason"],
                "answer_text": row["answer_text"],
                "notes": row["notes"],
                "monitored_at": row["monitored_at"],
            }
            for row in cursor.fetchall()
        ]
