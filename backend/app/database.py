import sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime
import os
import json


def local_now() -> str:
    """返回本地时间的ISO格式字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")



from datetime import datetime
from app.core.config import settings, ai_port_map, publish_port_map

DATABASE_PATH = Path(__file__).parent.parent / settings.DATABASE_PATH


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database():
    """Initialize database schema"""
    with get_db() as conn:
        cursor = conn.cursor()

        def _safe_add_column(table: str, col: str, col_type: str) -> None:
            """安全地为表添加列。

            只吞 "duplicate column name" 这种幂等场景；其他异常（权限错误、
            磁盘满、SQL 语法错误）必须抛出，避免生产迁移静默失败导致后续
            查询报 "no such column" 时无任何日志可查。
            """
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    return
                # 列已存在以外的 OperationalError 仍要抛出
                raise
            # 其他类型异常（DatabaseError 等）按外层 get_db 上下文回滚并抛出

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                role TEXT DEFAULT 'editor',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        # Brands table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                industry TEXT,
                products TEXT,
                target_audience TEXT,
                selling_points TEXT,
                competitors TEXT,
                owned_sites TEXT,
                aliases TEXT,
                target_platforms TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        # Competitors table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS competitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                industry TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        # Keywords table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                category TEXT,
                search_volume INTEGER,
                ai_status TEXT DEFAULT 'pending',
                ai_platform TEXT,
                last_tested TIMESTAMP,
                notes TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        # Monitor results table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                search_query TEXT NOT NULL,
                is_cited INTEGER DEFAULT 0,
                cited_sources TEXT,
                snippet TEXT,
                source_url TEXT,
                source_domain TEXT,
                screenshot_path TEXT,
                scan_mode TEXT DEFAULT 'manual',
                monitored_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                notes TEXT,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
            )
        """)

        # Monitor config table (per-brand scheduling)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL UNIQUE,
                scan_interval_hours INTEGER DEFAULT 24,
                platforms TEXT DEFAULT 'web,doubao',
                is_active INTEGER DEFAULT 1,
                last_full_scan TIMESTAMP,
                last_increment_scan TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        # Alerts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                keyword_id INTEGER,
                message TEXT,
                severity TEXT DEFAULT 'info',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        # P1 monitor run table: one user-triggered/scheduled batch.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                task_id INTEGER,
                platforms TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_keywords INTEGER DEFAULT 0,
                total_operations INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                verified_citation_count INTEGER DEFAULT 0,
                unverified_citation_count INTEGER DEFAULT 0,
                business_status TEXT DEFAULT 'pending',
                business_failure_reason TEXT,
                effective_source_count INTEGER DEFAULT 0,
                brand_mention_count INTEGER DEFAULT 0,
                brand_mention_rate REAL DEFAULT 0,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                error_msg TEXT,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        # P1 answer evidence table: one answer per platform and keyword.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                monitor_result_id INTEGER,
                brand_id INTEGER NOT NULL,
                keyword_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                prompt TEXT NOT NULL,
                answer_text TEXT,
                brand_mentioned INTEGER DEFAULT 0,
                mention_count INTEGER DEFAULT 0,
                rank_position INTEGER,
                verification_status TEXT DEFAULT 'not_cited',
                screenshot_path TEXT,
                raw_artifact_path TEXT,
                html_excerpt TEXT,
                elapsed_seconds REAL,
                failure_reason TEXT,
                captcha_status TEXT DEFAULT 'none',
                monitored_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (run_id) REFERENCES monitor_runs(id) ON DELETE SET NULL,
                FOREIGN KEY (monitor_result_id) REFERENCES monitor_results(id) ON DELETE SET NULL,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE CASCADE
            )
        """)

        # P1 source evidence table: every cited/search source with URL.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitor_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                answer_id INTEGER NOT NULL,
                title TEXT,
                url TEXT NOT NULL,
                domain TEXT,
                source_order INTEGER DEFAULT 0,
                is_brand_owned INTEGER DEFAULT 0,
                is_third_party INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (answer_id) REFERENCES monitor_answers(id) ON DELETE CASCADE
            )
        """)

        # P1 persistent platform browser/API session status.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS platform_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT UNIQUE NOT NULL,
                profile_dir TEXT,
                port INTEGER,
                status TEXT DEFAULT 'unknown',
                last_checked_at TIMESTAMP,
                captcha_required INTEGER DEFAULT 0,
                login_url TEXT,
                notes TEXT,
                risk_status TEXT DEFAULT 'normal',
                risk_reason TEXT,
                run_after TIMESTAMP,
                last_run_at TIMESTAMP,
                daily_run_date TEXT,
                daily_run_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS captcha_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                captcha_type TEXT,
                provider TEXT,
                status TEXT DEFAULT 'detected',
                manual_required INTEGER DEFAULT 0,
                page_url TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                resolved_at TIMESTAMP
            )
        """)

        # P2 keyword/content/publishing operating loop.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keyword_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                category TEXT,
                intent TEXT,
                score INTEGER DEFAULT 0,
                reason TEXT,
                source TEXT DEFAULT 'generator',
                status TEXT DEFAULT 'pending',
                created_keyword_id INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                FOREIGN KEY (created_keyword_id) REFERENCES keywords(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                keyword_id INTEGER,
                title TEXT NOT NULL,
                content_type TEXT NOT NULL,
                target_platform TEXT,
                target_keyword TEXT,
                body TEXT NOT NULL,
                media_assets_json TEXT,
                status TEXT DEFAULT 'draft',
                quality_score INTEGER DEFAULT 0,
                generation_notes TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                FOREIGN KEY (keyword_id) REFERENCES keywords(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publishing_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                account_name TEXT NOT NULL,
                profile_dir TEXT,
                port INTEGER,
                login_url TEXT,
                status TEXT DEFAULT 'unknown',
                notes TEXT,
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                UNIQUE(platform, account_name)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publishing_workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL UNIQUE,
                platform_name TEXT NOT NULL,
                login_url TEXT,
                publish_url TEXT,
                workflow_json TEXT,
                status TEXT DEFAULT 'draft',
                notes TEXT,
                last_tested_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publishing_workflow_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                tested_url TEXT,
                screenshot_path TEXT,
                screenshot_verified INTEGER DEFAULT 0,
                raw_artifact_path TEXT,
                raw_artifact_verified INTEGER DEFAULT 0,
                evidence_status TEXT DEFAULT 'unverified',
                failure_reason TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                completed_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publishing_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER NOT NULL,
                account_id INTEGER,
                brand_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                publish_mode TEXT DEFAULT 'manual_confirm',
                scheduled_at TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                published_url TEXT,
                published_url_verified INTEGER DEFAULT 0,
                screenshot_path TEXT,
                screenshot_verified INTEGER DEFAULT 0,
                raw_artifact_path TEXT,
                raw_artifact_verified INTEGER DEFAULT 0,
                summary_artifact_path TEXT,
                summary_artifact_verified INTEGER DEFAULT 0,
                evidence_status TEXT DEFAULT 'unverified',
                evidence_failure_reason TEXT,
                failure_reason TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (content_id) REFERENCES content_assets(id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                report_type TEXT DEFAULT 'scorecard',
                period_start TEXT,
                period_end TEXT,
                summary_json TEXT NOT NULL,
                artifact_path TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                strategy_json TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                llm_provider TEXT,
                llm_model TEXT,
                llm_usage_id INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                FOREIGN KEY (llm_usage_id) REFERENCES llm_usage(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS geo_strategy_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id INTEGER NOT NULL,
                brand_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                payload_json TEXT,
                requires_login INTEGER DEFAULT 0,
                cost_risk TEXT DEFAULT 'low',
                requires_confirmation INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (strategy_id) REFERENCES geo_strategies(id) ON DELETE CASCADE,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_api_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL,
                action TEXT NOT NULL,
                brand_id INTEGER,
                dry_run INTEGER DEFAULT 0,
                request_json TEXT,
                result_json TEXT,
                status TEXT DEFAULT 'pending',
                failure_reason TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                completed_at TIMESTAMP,
                FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                default_model TEXT NOT NULL,
                api_key TEXT,
                enabled INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 100,
                prompt_price_per_1k REAL DEFAULT 0,
                completion_price_per_1k REAL DEFAULT 0,
                last_test_status TEXT DEFAULT 'not_tested',
                last_test_message TEXT,
                last_tested_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                operation TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0,
                success INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (provider) REFERENCES llm_providers(provider) ON DELETE CASCADE
            )
        """)

        existing_brand_cols = [row[1] for row in cursor.execute("PRAGMA table_info(brands)")]
        for col in ["products", "target_audience", "selling_points", "competitors", "owned_sites"]:
            if col not in existing_brand_cols:
                cursor.execute(f"ALTER TABLE brands ADD COLUMN {col} TEXT")

        existing_report_cols = [row[1] for row in cursor.execute("PRAGMA table_info(geo_reports)")]
        if "artifact_path" not in existing_report_cols:
            cursor.execute("ALTER TABLE geo_reports ADD COLUMN artifact_path TEXT")
        for col, col_type in [("llm_provider", "TEXT"),
                               ("llm_model", "TEXT"),
                               ("llm_usage_id", "INTEGER"),
                               ("media_assets_json", "TEXT")]:
            if col not in existing_report_cols:
                _safe_add_column("geo_reports", col, col_type)

        existing_llm_cols = [row[1] for row in cursor.execute("PRAGMA table_info(llm_providers)")]
        for col, col_type in [("priority", "INTEGER DEFAULT 100"),
                               ("prompt_price_per_1k", "REAL DEFAULT 0"),
                               ("completion_price_per_1k", "REAL DEFAULT 0"),
                               ("last_test_status", "TEXT DEFAULT 'not_tested'"),
                               ("last_test_message", "TEXT"),
                               ("last_tested_at", "TIMESTAMP")]:
            if col not in existing_llm_cols:
                _safe_add_column("llm_providers", col, col_type)

        existing_idea_cols = [row[1] for row in cursor.execute("PRAGMA table_info(keyword_ideas)")]
        for col, col_type in [("llm_provider", "TEXT"),
                               ("llm_model", "TEXT"),
                               ("llm_usage_id", "INTEGER"),
                               ("generation_batch", "TEXT"),
                               ("batch_label", "TEXT"),
                               ("seed_text", "TEXT"),
                               ("is_duplicate", "INTEGER DEFAULT 0"),
                               ("brand_mention_type", "TEXT"),
                               ("geo_value_type", "TEXT")]:
            if col not in existing_idea_cols:
                _safe_add_column("keyword_ideas", col, col_type)

        existing_content_cols = [row[1] for row in cursor.execute("PRAGMA table_info(content_assets)")]
        for col, col_type in [("llm_provider", "TEXT"),
                               ("llm_model", "TEXT"),
                               ("llm_usage_id", "INTEGER"),
                               ("media_assets_json", "TEXT"),
                               ("source", "TEXT DEFAULT 'llm_generated'")]:
            if col not in existing_content_cols:
                _safe_add_column("content_assets", col, col_type)

        existing_strategy_action_cols = [row[1] for row in cursor.execute("PRAGMA table_info(geo_strategy_actions)")]
        for col, col_type in [("execution_result_json", "TEXT"),
                               ("failure_reason", "TEXT"),
                               ("executed_at", "TIMESTAMP")]:
            if col not in existing_strategy_action_cols:
                _safe_add_column("geo_strategy_actions", col, col_type)

        # Add new columns to monitor_results if missing (schema migration)
        existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(monitor_results)")]
        for col, col_type in [("snippet", "TEXT"), ("source_url", "TEXT"),
                               ("source_domain", "TEXT"), ("scan_mode", "TEXT DEFAULT 'manual'"),
                               ("answer_text", "TEXT"),
                               ("verification_status", "TEXT DEFAULT 'not_cited'"),
                               ("has_verified_source", "INTEGER DEFAULT 0"),
                               ("raw_artifact_path", "TEXT"),
                               ("failure_reason", "TEXT"),
                               ("elapsed_seconds", "REAL"),
                               ("captcha_status", "TEXT DEFAULT 'none'"),
                               ("total_results", "INTEGER DEFAULT 0"),
                               ("source_verified", "INTEGER DEFAULT 0"),
                               ("screenshot_verified", "INTEGER DEFAULT 0"),
                               ("raw_artifact_verified", "INTEGER DEFAULT 0"),
                               ("evidence_status", "TEXT DEFAULT 'unverified'"),
                               ("evidence_failure_reason", "TEXT")]:
            if col not in existing_cols:
                _safe_add_column("monitor_results", col, col_type)

        existing_answer_cols = [row[1] for row in cursor.execute("PRAGMA table_info(monitor_answers)")]
        for col, col_type in [("source_verified", "INTEGER DEFAULT 0"),
                               ("screenshot_verified", "INTEGER DEFAULT 0"),
                               ("raw_artifact_verified", "INTEGER DEFAULT 0"),
                               ("evidence_status", "TEXT DEFAULT 'unverified'"),
                               ("evidence_failure_reason", "TEXT")]:
            if col not in existing_answer_cols:
                _safe_add_column("monitor_answers", col, col_type)

        existing_run_cols = [row[1] for row in cursor.execute("PRAGMA table_info(monitor_runs)")]
        for col, col_type in [("business_status", "TEXT DEFAULT 'pending'"),
                               ("business_failure_reason", "TEXT"),
                               ("effective_source_count", "INTEGER DEFAULT 0"),
                               ("brand_mention_count", "INTEGER DEFAULT 0"),
                               ("brand_mention_rate", "REAL DEFAULT 0")]:
            if col not in existing_run_cols:
                _safe_add_column("monitor_runs", col, col_type)

        existing_source_cols = [row[1] for row in cursor.execute("PRAGMA table_info(monitor_sources)")]
        for col, col_type in [("source_verified", "INTEGER DEFAULT 0"),
                               ("check_status", "TEXT"),
                               ("checked_at", "TIMESTAMP")]:
            if col not in existing_source_cols:
                _safe_add_column("monitor_sources", col, col_type)

        existing_publish_cols = [row[1] for row in cursor.execute("PRAGMA table_info(publishing_tasks)")]
        for col, col_type in [("published_url_verified", "INTEGER DEFAULT 0"),
                               ("screenshot_verified", "INTEGER DEFAULT 0"),
                               ("raw_artifact_verified", "INTEGER DEFAULT 0"),
                               ("summary_artifact_path", "TEXT"),
                               ("summary_artifact_verified", "INTEGER DEFAULT 0"),
                               ("evidence_status", "TEXT DEFAULT 'unverified'"),
                               ("evidence_failure_reason", "TEXT")]:
            if col not in existing_publish_cols:
                _safe_add_column("publishing_tasks", col, col_type)

        existing_account_cols = [row[1] for row in cursor.execute("PRAGMA table_info(publishing_accounts)")]
        for col, col_type in [("publish_url", "TEXT")]:
            if col not in existing_account_cols:
                _safe_add_column("publishing_accounts", col, col_type)

        existing_brand_cols = [row[1] for row in cursor.execute("PRAGMA table_info(brands)")]
        for col, col_type in [("aliases", "TEXT")]:
            if col not in existing_brand_cols:
                _safe_add_column("brands", col, col_type)

        existing_platform_session_cols = [row[1] for row in cursor.execute("PRAGMA table_info(platform_sessions)")]
        for col, col_type in [
            ("risk_status", "TEXT DEFAULT 'normal'"),
            ("risk_reason", "TEXT"),
            ("run_after", "TIMESTAMP"),
            ("last_run_at", "TIMESTAMP"),
            ("daily_run_date", "TEXT"),
            ("daily_run_count", "INTEGER DEFAULT 0"),
        ]:
            if col not in existing_platform_session_cols:
                _safe_add_column("platform_sessions", col, col_type)

        existing_workflow_test_cols = [row[1] for row in cursor.execute("PRAGMA table_info(publishing_workflow_tests)")]
        for col, col_type in [("updated_at", "TIMESTAMP")]:
            if col not in existing_workflow_test_cols:
                _safe_add_column("publishing_workflow_tests", col, col_type)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitor_answers_run ON monitor_answers(run_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitor_answers_brand ON monitor_answers(brand_id, monitored_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitor_sources_answer ON monitor_sources(answer_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitor_results_brand_time ON monitor_results(brand_id, monitored_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_keyword_ideas_brand ON keyword_ideas(brand_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_assets_brand ON content_assets(brand_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_publishing_tasks_brand ON publishing_tasks(brand_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_publishing_workflow_tests_platform ON publishing_workflow_tests(platform, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_time ON llm_usage(provider, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_operation_time ON llm_usage(operation, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_strategies_brand ON geo_strategies(brand_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_strategy_actions_strategy ON geo_strategy_actions(strategy_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_geo_strategy_actions_brand ON geo_strategy_actions(brand_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_api_runs_brand ON agent_api_runs(brand_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_api_runs_action ON agent_api_runs(action, created_at)")

        default_llm_providers = [
            ("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-v4-pro", 10),
            ("qianwen", "千问/百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-max", 20),
            ("kimi", "Kimi", "https://api.moonshot.ai/v1", "kimi-k2.6", 30),
            ("minimax", "MiniMax 国内", "https://api.minimax.chat/v1", "MiniMax-M3", 40),
        ]
        for provider, display_name, base_url, model, priority in default_llm_providers:
            cursor.execute(
                """INSERT OR IGNORE INTO llm_providers
                   (provider, display_name, base_url, default_model, enabled, priority)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (provider, display_name, base_url, model, priority),
            )
            cursor.execute(
                """UPDATE llm_providers
                   SET display_name = ?, base_url = ?, default_model = ?, priority = ?, updated_at = ?
                   WHERE provider = ? AND (
                     default_model IN ('deepseek-chat','qwen-plus','moonshot-v1-8k','MiniMax-M1','kimi-k2.7-code')
                     OR base_url IN ('https://api.minimaxi.com/v1','https://api.moonshot.cn/v1')
                   )""",
                (display_name, base_url, model, priority, local_now(), provider),
            )

        default_publish_workflows = {
            "website": {
                "name": "官网/落地页",
                "login_url": "",
                "publish_url": "",
                "steps": ["generate_html", "manual_deploy", "verify_url", "post_publish_monitor"],
            },
            "ctrip_travel": {
                "name": "携程内容中心/旅拍",
                "login_url": "https://we.ctrip.com/account/login",
                "publish_url": "https://we.ctrip.com/account/login",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
            },
            "douyin": {
                "name": "抖音创作者中心",
                "login_url": "https://creator.douyin.com/",
                "publish_url": "https://creator.douyin.com/creator-micro/content/upload",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
            },
            "toutiao": {
                "name": "今日头条号",
                "login_url": "https://mp.toutiao.com/",
                "publish_url": "https://mp.toutiao.com/profile_v4/graphic/publish",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
            },
            "baijiahao": {
                "name": "百家号",
                "login_url": "https://baijiahao.baidu.com/",
                "publish_url": "https://baijiahao.baidu.com/builder/rc/edit?type=news&is_from_cms=1",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
                "selectors": {
                    "title": [".client_pages_edit_components_titleInput [contenteditable=\"true\"]"],
                    "body": ["frame:#ueditor_0 => body"],
                },
            },
            "sohu": {
                "name": "搜狐号",
                "login_url": "https://mp.sohu.com/",
                "publish_url": "https://mp.sohu.com/mpfe/v4/main/news/add",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
            },
            "zhihu": {
                "name": "知乎",
                "login_url": "https://www.zhihu.com/",
                "publish_url": "https://zhuanlan.zhihu.com/write",
                "steps": ["open_publish_url", "fill_title", "fill_body", "manual_publish", "verify_url"],
            },
            "xiaohongshu": {
                "name": "小红书",
                "login_url": "https://creator.xiaohongshu.com/",
                "publish_url": "https://creator.xiaohongshu.com/publish/publish",
                "steps": ["open_publish_url", "upload_or_fill", "manual_publish", "verify_url"],
            },
        }
        for platform, item in default_publish_workflows.items():
            cursor.execute(
                """INSERT OR IGNORE INTO publishing_workflows
                   (platform, platform_name, login_url, publish_url, workflow_json, status, notes)
                   VALUES (?, ?, ?, ?, ?, 'template', ?)""",
                (
                    platform,
                    item["name"],
                    item["login_url"],
                    item["publish_url"],
                    json.dumps(
                        {
                            "steps": item["steps"],
                            "selectors": item.get("selectors") or {},
                            "requires_manual_publish": True,
                        },
                        ensure_ascii=False,
                    ),
                    "默认发布入口；需按账号实际页面测试后固化选择器",
                ),
            )

        # Backfill old rows into the stricter P1 evidence vocabulary.
        cursor.execute("""
            UPDATE monitor_results
            SET verification_status = CASE
                    WHEN is_cited = 1 AND has_verified_source = 1 THEN 'verified_citation'
                    WHEN is_cited = 1 THEN 'unverified_citation'
                    ELSE 'not_cited'
                END,
                evidence_status = CASE
                    WHEN is_cited = 1 AND has_verified_source = 1 THEN 'verified'
                    ELSE COALESCE(evidence_status, 'unverified')
                END
            WHERE verification_status IS NULL
               OR verification_status = ''
               OR (verification_status = 'not_cited' AND is_cited = 1)
        """)
        cursor.execute("""
            UPDATE monitor_results
            SET answer_text = COALESCE(snippet, cited_sources)
            WHERE (answer_text IS NULL OR answer_text = '')
              AND (snippet IS NOT NULL OR cited_sources IS NOT NULL)
        """)
        cursor.execute("""
            UPDATE monitor_results
            SET captcha_status = 'detected'
            WHERE (captcha_status IS NULL OR captcha_status = 'none')
              AND notes LIKE '%验证码%'
        """)

        # Seed platform session rows. Ports avoid Hermes 9221-9229.
        # 端口统一来自 app.core.config（.env 的 GEO_PLATFORM_PORTS 可覆盖）。
        profile_root = os.path.expanduser(settings.PLATFORM_PROFILE_ROOT)
        port_map = ai_port_map()
        login_urls = {
            "doubao": "https://www.doubao.com/chat/",
            "qianwen": "https://www.qianwen.com/",
            "yuanbao": "https://yuanbao.tencent.com/chat",
        }
        for platform in ["doubao", "qianwen", "yuanbao"]:
            cursor.execute(
                """INSERT OR IGNORE INTO platform_sessions
                   (platform, profile_dir, port, status, login_url, notes)
                   VALUES (?, ?, ?, 'unknown', ?, '尚未检查登录态')""",
                (
                    platform,
                    os.path.join(profile_root, platform),
                    port_map.get(platform),
                    login_urls[platform],
                ),
            )
            # 已存在的行也要跟随配置刷新端口/目录（改了 .env 端口后重启即刻生效）
            cursor.execute(
                """UPDATE platform_sessions
                   SET port = ?, profile_dir = ?, updated_at = ?
                   WHERE platform = ?""",
                (port_map.get(platform), os.path.join(profile_root, platform), local_now(), platform),
            )

        # 发布账号端口跟随 GEO_PUBLISH_PORTS 刷新，避免 UI 上显示旧端口。
        # 这里刻意不写 updated_at：自动挑选发布账号（operations.py 的
        # ORDER BY updated_at DESC）和账号列表都按 updated_at 排序，启动时统一
        # 刷时间戳会让同平台多账号的时间戳并列，使"最近使用的账号"退化成最早
        # 插入的账号，并打乱 UI 列表顺序。只在端口确实变化时才更新该行。
        for platform, port in publish_port_map().items():
            cursor.execute(
                """UPDATE publishing_accounts
                   SET port = ?
                   WHERE platform = ? AND COALESCE(port, -1) != ?""",
                (port, platform, port),
            )

        conn.commit()


# 模块级自动初始化：使用一次性守卫避免重复 import（含多 worker、测试、reload）
# 触发冗余建表/迁移和 database is locked。
_INIT_DONE = False


def ensure_init_database() -> None:
    """显式触发数据库初始化（幂等，多次调用只执行一次）。

    历史实现是在模块底部直接 init_database()，每次 import 都会执行
    建表 + ~15 段 ALTER 迁移 + 种子数据；多 worker 并发或 reload 场景下
    易触发 "database is locked"。改为外部显式调用（如 FastAPI lifespan）。
    """
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True
    init_database()


# 兼容旧调用方：保留 import 时即就绪的语义，但用守卫去重。
# 建议后续在 FastAPI lifespan 中改为显式 ensure_init_database() 调用。
ensure_init_database()
