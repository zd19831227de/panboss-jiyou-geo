"""Base class for all platform monitors"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from pathlib import Path


@dataclass
class CitationResult:
    is_cited: bool = False
    cited_sources: Optional[str] = None  # comma-separated source descriptions
    snippet: Optional[str] = None        # matched text excerpt
    source_url: Optional[str] = None     # first matching URL
    source_domain: Optional[str] = None  # first matching domain
    screenshot_path: Optional[str] = None
    notes: Optional[str] = None
    total_results: int = 0


class BaseMonitor(ABC):
    """Abstract base for all AI/platform monitors"""

    platform: str = "base"

    @abstractmethod
    def search(self, brand_name: str, keyword: str) -> CitationResult:
        ...

    def format_query(self, brand_name: str, keyword: str) -> str:
        return f"{keyword} {brand_name}"
