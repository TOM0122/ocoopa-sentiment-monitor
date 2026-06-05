from __future__ import annotations

from typing import Dict, List

from .db import Database


class SourceHealthMonitor:
    def __init__(self, db: Database):
        self.db = db

    def check(self) -> List[Dict[str, object]]:
        return self.db.unhealthy_sources()

