from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, List, Optional

from ..models import RawItem, SourceConfig


class Fetcher(ABC):
    @abstractmethod
    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        raise NotImplementedError

