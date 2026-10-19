"""
多引擎网页搜索监测 — 纯HTTP请求
  引擎1: 百度 (主, 中文SEO最准)
  引擎2: 必应 (备用)
  引擎3: DuckDuckGo (最后备用)

  反爬策略:
    - 每次请求前sleep 3-8秒随机间隔 (低频防止触发验证码)
    - 随机User-Agent轮换
    - 检测验证码，自动跳转下一引擎
    - 请求重试(最多2次)

  引用源: 搜索结果页URL + 域名 + 标题 全部保存
"""
import re
import time
import random
import urllib.parse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote
from typing import Optional
from bs4 import BeautifulSoup
import requests
from app.services.monitor.base import BaseMonitor, CitationResult

# BeautifulSoup 解析器：优先 lxml（快），但 lxml 不是 pip 默认依赖，
# 缺失时退化为标准库 html.parser，避免整条网页搜索监测静默失败。
def _best_parser() -> str:
    try:
        import lxml  # noqa: F401
        return "lxml"
    except Exception:
        return "html.parser"


HTML_PARSER = _best_parser()

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# 请求间隔控制 — 同引擎至少间隔8秒，防止触发验证码
_last_request_time = {}

def _rate_limit(engine: str, min_interval: float = 8.0):
    """限流: 同一引擎请求间隔至少 min_interval 秒"""
    global _last_request_time
    last = _last_request_time.get(engine, 0)
    elapsed = time.time() - last
    if elapsed < min_interval:
        sleep_time = min_interval - elapsed + random.uniform(0.5, 3.0)
        time.sleep(sleep_time)
    _last_request_time[engine] = time.time()

def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

def _is_captcha(text: str) -> bool:
    """检测页面是否触发了验证码/反爬"""
    signals = ['captcha', '验证码', 'reCAPTCHA', 'verify', 'security check',
               '请输入验证码', '图形验证码', '滑动验证', 'antispider',
               '检测到有异常请求', '访问受限', '请开启JavaScript']
    return any(s in text.lower() for s in signals)

class WebSearchMonitor(BaseMonitor):
    platform = "web"

    def __init__(self, headless: bool = True):
        self._session = requests.Session()

    def search(self, brand_name: str, keyword: str) -> CitationResult:
        """三引擎级联搜索: 百度 → 必应 → DuckDuckGo"""
        # 百度 — 主引擎
        _rate_limit("baidu")
        result1 = self._baidu_search(brand_name, keyword)
        if result1.total_results > 0:
            return result1

        # 必应 — 备用
        _rate_limit("bing")
        result2 = self._bing_search(brand_name, keyword)
        if result2.total_results > 0:
            return result2

        # DuckDuckGo — 最后备用（通常被屏蔽）
        _rate_limit("ddg")
        result3 = self._ddg_search(brand_name, keyword)
        if result3.total_results > 0:
            return result3

        # 全部失败，返回notes最详细的那个
        for r in [result1, result2, result3]:
            if r.notes and "异常" not in r.notes and "验证码" not in r.notes:
                return r
        return result1

    def _baidu_search(self, brand_name: str, keyword: str) -> CitationResult:
        """百度搜索 — 主引擎, 中文SEO检测最准确"""
        query = quote(self.format_query(brand_name, keyword))
        result = CitationResult()
        try:
            resp = self._session.get(
                f"https://www.baidu.com/s?wd={query}&ie=utf-8&rn=15",
                headers=_headers(), timeout=25,
            )
            if _is_captcha(resp.text) or len(resp.text) < 5000:
                result.notes = "百度触发了验证码"
                return result

            soup = BeautifulSoup(resp.text, HTML_PARSER)
            items = soup.select('.result, .result-op, .c-container')
            if not items:
                items = soup.select('div[class*="result"]')

            cited = []; total = 0; domains = set()
            first_url = first_domain = first_snippet = None
            brand_lower = brand_name.lower()

            for item in items[:15]:
                h3 = item.select_one('h3')
                if not h3: continue
                link = h3.select_one('a')
                if not link: continue
                title = link.get_text(strip=True)
                href = link.get('href', '')
                if not href or not title: continue
                if href.startswith('/'):
                    href = 'https://www.baidu.com' + href

                total += 1
                try:
                    domain = urlparse(href).netloc
                    domains.add(domain)
                except Exception: domain = None

                snippet_el = item.select_one('.c-abstract, .c-span-last, .content-center, .c-color-gray')
                snippet = snippet_el.get_text(strip=True)[:200] if snippet_el else ""

                has_brand = brand_lower in title.lower() or brand_lower in snippet.lower()
                if has_brand:
                    cited.append(f"{title}" + (f" — {snippet[:100]}" if snippet else ""))
                    if first_url is None:
                        first_url = href
                        first_domain = domain
                        first_snippet = snippet or title[:200]

            result.total_results = total
            result.is_cited = len(cited) > 0
            result.cited_sources = " | ".join(cited[:5]) if cited else None
            result.source_url = first_url
            result.source_domain = first_domain
            result.snippet = first_snippet
            result.notes = f"百度 | 结果:{total} | 收录:{len(cited)}"
            if domains:
                result.notes += f" | 域名:{','.join(sorted(domains)[:5])}"

        except Exception as e:
            result.notes = f"百度异常: {str(e)[:80]}"
        return result

    def _bing_search(self, brand_name: str, keyword: str) -> CitationResult:
        """必应搜索 — 备用"""
        query = quote(self.format_query(brand_name, keyword))
        result = CitationResult()
        try:
            resp = self._session.get(
                f"https://www.bing.com/search?q={query}&count=15",
                headers=_headers(), timeout=20,
            )
            if _is_captcha(resp.text) or len(resp.text) < 2000:
                result.notes = "必应触发了验证码"
                return result

            soup = BeautifulSoup(resp.text, HTML_PARSER)
            items = soup.select('li.b_algo')
            cited = []; total = 0; domains = set()
            first_url = first_domain = first_snippet = None
            brand_lower = brand_name.lower()

            for item in items[:15]:
                h2 = item.select_one('h2')
                if not h2: continue
                link = h2.select_one('a')
                title = h2.get_text(strip=True)
                href = link.get('href', '') if link else ''
                total += 1
                if href:
                    try: domain = urlparse(href).netloc; domains.add(domain)
                    except Exception: domain = None
                else: domain = None
                snippet_el = item.select_one('.b_caption p')
                snippet = snippet_el.get_text(strip=True)[:200] if snippet_el else ""
                has_brand = brand_lower in title.lower() or brand_lower in snippet.lower()
                if has_brand:
                    cited.append(f"{title}" + (f" — {snippet[:100]}" if snippet else ""))
                    if first_url is None:
                        first_url = href; first_domain = domain
                        first_snippet = snippet or title[:200]

            result.total_results = total
            result.is_cited = len(cited) > 0
            result.cited_sources = " | ".join(cited[:5]) if cited else None
            result.source_url = first_url; result.source_domain = first_domain
            result.snippet = first_snippet
            result.notes = f"必应 | 结果:{total} | 收录:{len(cited)}"
            if domains:
                result.notes += f" | 域名:{','.join(sorted(domains)[:5])}"
        except Exception as e:
            result.notes = f"必应异常: {str(e)[:80]}"
        return result

    def _ddg_search(self, brand_name: str, keyword: str) -> CitationResult:
        """DuckDuckGo — 最后备用"""
        query = quote(self.format_query(brand_name, keyword))
        result = CitationResult()
        try:
            resp = self._session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=_headers(), timeout=15,
            )
            soup = BeautifulSoup(resp.text, HTML_PARSER)
            result_items = soup.select('.result') or soup.find_all('div', class_='result')
            cited = []; total = 0; domains = set()
            first_url = first_domain = first_snippet = None
            brand_lower = brand_name.lower()

            for item in result_items[:20]:
                title_el = item.select_one('.result__title a, .result__a, h2 a, a')
                if not title_el: continue
                title = title_el.get_text(strip=True)
                href = title_el.get('href', '')
                if not title or len(title) < 5 or not href: continue
                if 'duckduckgo.com/l/' in href:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                    real_urls = parsed.get('uddg', [])
                    if real_urls: href = urllib.parse.unquote(real_urls[0])
                    else: continue
                total += 1
                try: domain = urlparse(href).netloc; domains.add(domain)
                except Exception: domain = None
                snippet_el = item.select_one('.result__snippet')
                snippet = snippet_el.get_text(strip=True)[:200] if snippet_el else ""
                has_brand = brand_lower in title.lower() or brand_lower in snippet.lower()
                if has_brand:
                    cited.append(f"{title}" + (f" — {snippet[:100]}" if snippet else ""))
                    if first_url is None:
                        first_url = href; first_domain = domain
                        first_snippet = snippet or title[:200]

            result.total_results = total
            result.is_cited = len(cited) > 0
            result.cited_sources = " | ".join(cited[:5]) if cited else None
            result.source_url = first_url; result.source_domain = first_domain
            result.snippet = first_snippet
            result.notes = f"DuckDuckGo | 结果:{total} | 收录:{len(cited)}"
            if domains:
                result.notes += f" | 域名:{','.join(sorted(domains)[:5])}"
        except Exception as e:
            result.notes = f"DuckDuckGo异常: {str(e)[:80]}"
        return result

    def format_query(self, brand_name: str, keyword: str) -> str:
        # 百度搜索: 品牌名+关键词 一起搜
        return f"{keyword} {brand_name}"

    def close(self):
        try: self._session.close()
        except Exception: pass
    def _close_browser(self): pass

def get_web_monitor() -> WebSearchMonitor:
    return WebSearchMonitor()
